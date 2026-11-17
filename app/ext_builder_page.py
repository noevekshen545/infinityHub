#!/usr/bin/env python3
"""Visual Extension Builder — no-code конструктор расширений."""

PAGE_EXT_BUILDER = r'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Visual Extension Builder — Infinity</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<link rel="stylesheet" href="/static/fonts/google-fonts.css">
<link rel="stylesheet" href="/static/monaco/min/vs/editor/editor.main.css">
<script src="/static/monaco/min/vs/loader.js"></script>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
--bg:#f8fafc;--bg2:#ffffff;--bg3:#f1f5f9;--bg4:#e2e8f0;
--text:#0f172a;--text1:#1e293b;--text2:#475569;--text3:#94a3b8;
--accent:#6366f1;--accent2:#4f46e5;--accent-light:#eef2ff;--accent-glow:rgba(99,102,241,0.15);
--green:#10b981;--green-light:#ecfdf5;--red:#ef4444;--orange:#f97316;--purple:#a855f7;
--border:#e2e8f0;
--shadow-sm:0 1px 3px rgba(0,0,0,0.06);--shadow:0 4px 16px rgba(0,0,0,0.08);--shadow-lg:0 8px 32px rgba(0,0,0,0.10);
--radius:12px;--radius-sm:8px;--radius-lg:16px;--radius-xl:24px;
--font:'Inter',system-ui,-apple-system,sans-serif;--font-mono:'JetBrains Mono','Cascadia Code',monospace;
--tr:all 0.25s cubic-bezier(0.4,0,0.2,1)
}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh}
::selection{background:var(--accent);color:#fff}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--accent)}

/* Top bar */
.topbar{position:sticky;top:0;z-index:100;background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-bottom:1px solid rgba(0,0,0,0.06);padding:0 24px;display:flex;align-items:center;height:60px;gap:16px}
.topbar .logo{font-size:18px;font-weight:700;background:linear-gradient(135deg,#6366f1,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent;display:flex;align-items:center;gap:8px}
.topbar .logo i{-webkit-text-fill-color:initial;color:var(--accent);font-size:20px}
.topbar .back{padding:6px 14px;border-radius:var(--radius-sm);background:var(--bg3);border:1px solid transparent;color:var(--text2);cursor:pointer;font-size:13px;text-decoration:none;transition:var(--tr);display:flex;align-items:center;gap:6px}
.topbar .back:hover{background:var(--bg4);color:var(--text)}
.topbar .subtitle{font-size:13px;color:var(--text3);margin-left:auto}

/* Progress bar */
.progress-wrap{background:var(--bg2);border-bottom:1px solid rgba(0,0,0,0.04);padding:16px 24px 0;position:sticky;top:60px;z-index:99}
.progress{display:flex;gap:0;max-width:900px;margin:0 auto}
.prog-step{flex:1;text-align:center;padding:8px 4px 12px;font-size:11px;font-weight:600;color:var(--text3);cursor:pointer;position:relative;transition:var(--tr);border-bottom:3px solid var(--bg4)}
.prog-step:hover{color:var(--text2)}
.prog-step.active{color:var(--accent);border-bottom-color:var(--accent)}
.prog-step.done{color:var(--green);border-bottom-color:var(--green)}
.prog-step .num{display:inline-flex;width:22px;height:22px;border-radius:50%;background:var(--bg3);align-items:center;justify-content:center;font-size:10px;font-weight:700;margin-right:6px;transition:var(--tr)}
.prog-step.active .num{background:var(--accent);color:#fff}
.prog-step.done .num{background:var(--green);color:#fff}

/* Content */
.container{max-width:960px;margin:0 auto;padding:28px 24px 80px}

/* Step panels */
.step-panel{display:none;animation:fadeIn 0.35s ease}
.step-panel.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}

.step-header{margin-bottom:24px}
.step-header h2{font-size:22px;font-weight:700;display:flex;align-items:center;gap:10px}
.step-header p{font-size:14px;color:var(--text2);margin-top:6px;line-height:1.5}

/* Cards */
.card{background:var(--bg2);border:1px solid rgba(0,0,0,0.06);border-radius:var(--radius-lg);padding:24px;margin-bottom:16px;box-shadow:var(--shadow-sm);transition:var(--tr)}
.card:hover{box-shadow:var(--shadow)}
.card-title{font-size:15px;font-weight:600;margin-bottom:14px;display:flex;align-items:center;gap:8px}

/* Form elements */
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;font-weight:600;color:var(--text2);margin-bottom:5px}
.form-group .hint{font-size:11px;color:var(--text3);margin-top:3px}
input[type="text"],input[type="url"],input[type="number"],select,textarea{width:100%;padding:10px 14px;background:var(--bg);border:2px solid var(--bg4);border-radius:var(--radius-sm);color:var(--text);font-size:14px;font-family:var(--font);outline:none;transition:var(--tr)}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
textarea{resize:vertical;min-height:80px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}

/* Module grid */
.module-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.module-card{padding:16px 12px;background:var(--bg);border:2px solid transparent;border-radius:var(--radius);cursor:pointer;text-align:center;transition:var(--tr);position:relative}
.module-card:hover{border-color:var(--accent-light);background:var(--accent-light)}
.module-card.selected{border-color:var(--accent);background:var(--accent-light);box-shadow:0 0 0 3px var(--accent-glow)}
.module-card .mod-icon{font-size:28px;color:var(--accent);margin-bottom:8px;display:block}
.module-card .mod-name{font-size:13px;font-weight:600}
.module-card .mod-desc{font-size:10px;color:var(--text3);margin-top:3px}
.module-card .check{position:absolute;top:6px;right:6px;width:18px;height:18px;border-radius:50%;background:var(--bg4);display:none;align-items:center;justify-content:center;font-size:9px;color:#fff}
.module-card.selected .check{display:flex;background:var(--accent)}
.module-card.special{border-style:dashed;border-color:var(--bg4)}
.module-card.special:hover{border-color:var(--purple);background:rgba(168,85,247,0.06)}
.module-card.special.selected{border-color:var(--purple);background:rgba(168,85,247,0.08)}
.module-card.special.selected .check{background:var(--purple)}

/* Hooks list */
.hooks-group{margin-bottom:20px}
.hooks-group .group-title{font-size:14px;font-weight:600;color:var(--text);margin-bottom:10px;display:flex;align-items:center;gap:8px;padding-bottom:6px;border-bottom:1px solid var(--bg4)}
.hook-item{display:flex;align-items:center;gap:12px;padding:8px 12px;border-radius:var(--radius-sm);cursor:pointer;transition:var(--tr)}
.hook-item:hover{background:var(--bg3)}
.hook-item input[type="checkbox"]{width:18px;height:18px;accent-color:var(--accent);cursor:pointer;flex-shrink:0}
.hook-item .hook-label{font-size:14px;font-weight:500}
.hook-item .hook-desc{font-size:12px;color:var(--text3);margin-left:auto;text-align:right;max-width:300px}

/* Action builder */
.action-area{margin-top:8px;margin-left:30px;border-left:2px solid var(--accent-light);padding-left:14px}
.action-card{background:var(--bg);border:1px solid var(--bg4);border-radius:var(--radius-sm);padding:10px 12px;margin-bottom:8px;transition:var(--tr)}
.action-card:hover{border-color:var(--accent-light)}
.action-card .ac-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.action-card .ac-head select{flex:1;padding:4px 8px;border-radius:6px;border:1px solid var(--bg4);background:var(--bg2);color:var(--text1);font-size:12px}
.action-card .ac-head .del-action{background:none;border:none;color:var(--red);cursor:pointer;font-size:14px;padding:2px 6px;border-radius:4px}
.action-card .ac-head .del-action:hover{background:rgba(239,68,68,0.15)}
.move-btn{background:none;border:none;color:var(--text2);cursor:pointer;font-size:12px;padding:2px 4px;border-radius:3px}
.move-btn:hover{background:var(--bg3);color:var(--accent)}
.action-card .ac-params{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.action-card .ac-params .full-w{grid-column:1/-1}
.action-card .ac-params label{font-size:11px;color:var(--text2);display:block;margin-bottom:2px}
.action-card .ac-params input,.action-card .ac-params select,.action-card .ac-params textarea{width:100%;padding:5px 8px;border-radius:5px;border:1px solid var(--bg4);background:var(--bg2);color:var(--text1);font-size:12px;box-sizing:border-box}
.action-card .ac-params textarea{min-height:40px;resize:vertical;font-family:monospace}
.add-action-btn{padding:4px 12px;font-size:11px;border-radius:6px;border:1px dashed var(--bg4);background:none;color:var(--text2);cursor:pointer;transition:var(--tr);margin-top:4px}
.add-action-btn:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-light)}
.action-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}
.action-badge.log{background:#1a3a2a;color:#4ade80}
.action-badge.notify{background:#3a2a1a;color:#fbbf24}
.action-badge.api_call{background:#1a2a3a;color:#60a5fa}
.action-badge.modify_dom{background:#2a1a3a;color:#c084fc}
.action-badge.redirect{background:#3a1a1a;color:#f87171}
.action-badge.custom{background:#2a2a2a;color:#d1d5db}

/* Template grid */
.template-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-top:6px}
.template-card{padding:12px 8px;border:2px solid var(--bg4);border-radius:var(--radius-sm);cursor:pointer;text-align:center;transition:var(--tr);background:var(--bg)}
.template-card:hover{border-color:var(--accent);background:var(--accent-light);transform:translateY(-2px)}
.template-card .tc-icon{font-size:22px;color:var(--accent);margin-bottom:6px}
.template-card .tc-name{font-size:12px;font-weight:600;margin-bottom:2px}
.template-card .tc-desc{font-size:10px;color:var(--text3);line-height:1.3}

/* Snippet modal */
.snip-modal{position:fixed;top:0;left:0;right:0;bottom:0;z-index:2000;background:rgba(0,0,0,0.65);display:flex;align-items:center;justify-content:center}
.snip-modal-content{background:#1a1a2e;border-radius:12px;width:600px;max-width:90vw;max-height:80vh;overflow:hidden;display:flex;flex-direction:column}
.snip-modal-header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--bg4)}
.snip-modal-header h3{font-size:16px}
.snip-modal-header button{background:none;border:none;color:var(--text2);font-size:20px;cursor:pointer}
.snip-list{overflow-y:auto;padding:10px 14px;flex:1}
.snip-item{padding:10px 12px;margin-bottom:6px;border-radius:8px;border:1px solid var(--bg4);cursor:pointer;transition:var(--tr)}
.snip-item:hover{border-color:var(--accent);background:var(--accent-light)}
.snip-item .snip-label{font-size:13px;font-weight:600}
.snip-item .snip-desc{font-size:11px;color:var(--text3);margin-top:2px}
.snip-item .snip-code{display:none}

/* API browser improvements */
.api-module-filter{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;padding:0 2px}
.api-mod-filter-btn{padding:4px 12px;border-radius:12px;border:1px solid var(--bg4);background:var(--bg);color:var(--text2);font-size:11px;cursor:pointer;transition:var(--tr)}
.api-mod-filter-btn:hover{border-color:var(--accent-light);color:var(--accent)}
.api-mod-filter-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.api-item{position:relative}
.api-item .api-copy{position:absolute;right:30px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text3);cursor:pointer;font-size:12px;padding:4px 6px;border-radius:4px;opacity:0;transition:var(--tr)}
.api-item:hover .api-copy{opacity:1}
.api-item .api-copy:hover{color:var(--accent);background:var(--accent-light)}
.api-item .api-name{font-size:12px;color:var(--text3);margin-left:8px;flex:1}
.api-item .curated-badge{font-size:9px;padding:1px 6px;border-radius:3px;background:var(--bg4);color:var(--text3);margin-left:6px}
.api-empty-state{padding:24px;text-align:center;color:var(--text3)}
.api-empty-state .big-icon{font-size:40px;margin-bottom:12px;display:block;color:var(--bg4)}
.api-empty-state h4{font-size:15px;margin-bottom:6px;color:var(--text2)}
.api-empty-state p{font-size:12px;margin-bottom:10px;line-height:1.5}
.api-empty-state .btn{padding:6px 16px;font-size:12px;border-radius:8px}
.api-manual-form{background:var(--bg);border-radius:var(--radius-sm);padding:12px;margin-top:8px;display:none;border:1px solid var(--bg4)}
.api-manual-form.show{display:block}
.api-manual-form .form-row{display:flex;gap:8px;margin-bottom:8px}
.api-manual-form input,.api-manual-form select{padding:6px 10px;border-radius:6px;border:1px solid var(--bg4);background:var(--bg2);color:var(--text1);font-size:12px;flex:1}
.api-manual-form .btn{padding:6px 14px;font-size:12px;border-radius:6px}
.api-opt-group{font-size:11px;font-weight:700;color:var(--text3);padding:8px 12px 4px;text-transform:uppercase;letter-spacing:0.5px;background:var(--bg2);position:sticky;top:0;z-index:1}

/* Permission toggles */
.perm-item{display:flex;align-items:center;gap:14px;padding:12px 14px;border-radius:var(--radius-sm);transition:var(--tr);cursor:pointer}
.perm-item:hover{background:var(--bg3)}
.perm-item .toggle{position:relative;width:40px;height:22px;flex-shrink:0;background:var(--bg4);border-radius:11px;transition:var(--tr);cursor:pointer}
.perm-item .toggle.active{background:var(--accent)}
.perm-item .toggle::after{content:'';position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:var(--tr);box-shadow:0 1px 3px rgba(0,0,0,0.2)}
.perm-item .toggle.active::after{left:21px}
.perm-item .perm-info{flex:1}
.perm-item .perm-name{font-size:13px;font-weight:600;color:var(--text)}
.perm-item .perm-desc{font-size:12px;color:var(--text3);margin-top:1px}

/* Icon grid */
.icon-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(44px,1fr));gap:6px;max-height:200px;overflow-y:auto;padding:4px}
.icon-grid .icon-opt{width:44px;height:44px;display:flex;align-items:center;justify-content:center;background:var(--bg);border:2px solid transparent;border-radius:var(--radius-sm);cursor:pointer;font-size:18px;color:var(--text2);transition:var(--tr)}
.icon-grid .icon-opt:hover{border-color:var(--accent-light);background:var(--accent-light);color:var(--accent)}
.icon-grid .icon-opt.selected{border-color:var(--accent);background:var(--accent-light);color:var(--accent);box-shadow:0 0 0 2px var(--accent-glow)}

/* Preview card */
.preview-card{background:var(--bg);border:1px solid rgba(0,0,0,0.06);border-radius:var(--radius);padding:16px;display:flex;align-items:center;gap:14px;max-width:360px}
.preview-card .pv-icon{width:44px;height:44px;border-radius:var(--radius-sm);background:linear-gradient(135deg,var(--accent-light),rgba(168,85,247,0.1));display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--accent);flex-shrink:0}
.preview-card .pv-info{flex:1;min-width:0}
.preview-card .pv-name{font-size:14px;font-weight:600}
.preview-card .pv-name .ver{font-size:10px;padding:2px 6px;background:var(--bg4);border-radius:4px;color:var(--text3);margin-left:6px;font-weight:400}
.preview-card .pv-author{font-size:11px;color:var(--text3)}
.preview-card .pv-desc{font-size:11px;color:var(--text2);margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

/* Config schema editor */
.config-field{display:flex;gap:8px;align-items:center;padding:8px 10px;background:var(--bg);border-radius:var(--radius-sm);margin-bottom:6px;border:1px solid rgba(0,0,0,0.04)}
.config-field input,.config-field select{flex:1;padding:6px 10px;background:var(--bg2);border:1px solid var(--bg4);border-radius:6px;font-size:12px;color:var(--text);outline:none}
.config-field input:focus,.config-field select:focus{border-color:var(--accent)}
.config-field .del-field{padding:4px 8px;border:none;background:transparent;color:var(--text3);cursor:pointer;border-radius:4px;font-size:14px;transition:var(--tr)}
.config-field .del-field:hover{background:rgba(239,68,68,0.1);color:var(--red)}

/* API browser */
.api-search{padding:10px 14px;background:var(--bg);border:2px solid var(--bg4);border-radius:var(--radius-sm);margin-bottom:14px;display:flex;gap:10px;align-items:center}
.api-search input{flex:1;border:none;background:transparent;outline:none;font-size:13px;color:var(--text);font-family:var(--font)}
.api-search input::placeholder{color:var(--text3)}
.api-list{max-height:400px;overflow-y:auto}
.api-item{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:var(--radius-sm);cursor:pointer;transition:var(--tr);font-family:var(--font-mono);font-size:12px}
.api-item:hover{background:var(--bg3)}
.api-item .method-tag{font-size:9px;font-weight:700;padding:2px 7px;border-radius:5px;color:#fff;text-transform:uppercase;min-width:44px;text-align:center;flex-shrink:0}
.api-item .method-tag.get{background:#238636}
.api-item .method-tag.post{background:#d29922}
.api-item .method-tag.put{background:#8b5cf6}
.api-item .method-tag.delete{background:#da3633}
.api-item .method-tag.patch{background:#f97316}
.api-item .api-path{color:var(--text);font-weight:500}
.api-item .api-name{color:var(--text3);font-size:11px;margin-left:auto;font-family:var(--font)}
.api-item .api-pin{color:var(--text3);cursor:pointer;padding:2px 6px;border-radius:4px;transition:var(--tr)}
.api-item .api-pin:hover{color:var(--accent)}
.api-item .api-pin.pinned{color:var(--accent)}

/* Code preview */
.code-preview{background:#1e293b;border-radius:var(--radius);overflow:hidden;margin-bottom:14px}
.code-preview .code-header{display:flex;align-items:center;gap:8px;padding:8px 14px;background:rgba(255,255,255,0.04);border-bottom:1px solid rgba(255,255,255,0.06)}
.code-preview .code-header .file-tab{padding:4px 12px;border-radius:6px;font-size:11px;color:var(--text3);cursor:pointer;font-family:var(--font-mono);transition:var(--tr)}
.code-preview .code-header .file-tab:hover{color:#fff;background:rgba(255,255,255,0.06)}
.code-preview .code-header .file-tab.active{color:#fff;background:rgba(255,255,255,0.08)}
.code-preview pre{margin:0;padding:16px;overflow-x:auto;font-family:var(--font-mono);font-size:12px;line-height:1.6;color:#e2e8f0;max-height:400px;overflow-y:auto}

/* Nav buttons */
.nav-buttons{display:flex;gap:10px;justify-content:space-between;margin-top:28px;padding-top:20px;border-top:1px solid rgba(0,0,0,0.06)}
.btn{padding:10px 22px;border-radius:var(--radius-sm);font-size:14px;font-weight:600;cursor:pointer;transition:var(--tr);display:inline-flex;align-items:center;gap:8px;border:2px solid transparent;font-family:var(--font)}
.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-primary:hover{background:var(--accent2);border-color:var(--accent2);box-shadow:0 4px 12px var(--accent-glow);transform:translateY(-1px)}
.btn-secondary{background:var(--bg2);color:var(--text);border-color:var(--bg4)}
.btn-secondary:hover{background:var(--bg3);border-color:var(--text3)}
.btn-success{background:var(--green);color:#fff;border-color:var(--green)}
.btn-success:hover{background:#059669;box-shadow:0 4px 12px rgba(16,185,129,0.3);transform:translateY(-1px)}
.btn-info{background:#2d5a8a;color:#fff;border-color:#2d5a8a}
.btn-info:hover{background:#3a6ea0}
.btn-danger{background:#dc2626;color:#fff;border-color:#dc2626}
.btn-danger:hover{background:#b91c1c}
.btn-warning{background:#b45309;color:#fff;border-color:#b45309}
.btn-warning:hover{background:#d97706}
.btn-sm{padding:4px 12px;font-size:12px;border-radius:6px}
.btn-ghost{background:transparent;color:var(--text2);border-color:transparent}
.btn-ghost:hover{background:var(--bg3);color:var(--text)}
.btn-lg{padding:12px 28px;font-size:15px;border-radius:var(--radius)}
.btn:disabled{opacity:0.5;cursor:not-allowed;transform:none!important}

/* New module form */
.new-module-form{padding:16px;background:var(--bg);border-radius:var(--radius);border:2px dashed var(--purple);margin-top:12px;display:none}
.new-module-form.show{display:block}
.new-module-form .form-row{grid-template-columns:1fr 1fr 1fr}
.custom-hook-row{display:flex;gap:8px;align-items:center;margin-bottom:6px}
.custom-hook-row input{flex:1}
.custom-hook-row .del-hook{padding:4px 8px;border:none;background:transparent;color:var(--text3);cursor:pointer;border-radius:4px;transition:var(--tr)}
.custom-hook-row .del-hook:hover{background:rgba(239,68,68,0.1);color:var(--red)}

/* Frontend config */
.frontend-opts{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.frontend-opt{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--bg);border:1px solid rgba(0,0,0,0.04);border-radius:var(--radius-sm);cursor:pointer;transition:var(--tr)}
.frontend-opt:hover{border-color:var(--accent-light)}
.frontend-opt input[type="checkbox"]{accent-color:var(--accent);width:16px;height:16px}

/* Module pages */
.mp-item{padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:8px}
.mp-item input,.mp-item select,.mp-item textarea{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:13px}
.mp-item input:focus,.mp-item select:focus,.mp-item textarea:focus{border-color:var(--accent);outline:none}
.mp-item textarea::placeholder{color:var(--text3)}

/* Hint boxes */
.hint-box{display:flex;align-items:flex-start;gap:8px;padding:10px 14px;background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.2);border-radius:var(--radius-sm);font-size:12px;color:var(--text2);line-height:1.5;margin-top:10px;overflow-wrap:break-word;word-break:break-word;white-space:normal}
.hint-box i{color:var(--accent);margin-top:2px;flex-shrink:0}
.hint-box code{background:var(--bg3);padding:1px 5px;border-radius:4px;font-size:11px;color:var(--accent);word-break:break-all}
.hint-box .flex-break{flex-basis:100%;height:0}
/* Dependencies */
.dep-row{display:flex;gap:8px;align-items:center;margin-bottom:6px}
.dep-row input{flex:1}

/* Tags/badges */
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600}
.badge-module{background:var(--accent-light);color:var(--accent)}
.badge-hook{background:var(--green-light);color:var(--green)}

/* Summary */
.summary-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.summary-item{padding:14px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid rgba(0,0,0,0.04)}
.summary-item .s-label{font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text3);font-weight:600;margin-bottom:6px}
.summary-item .s-value{font-size:14px;color:var(--text)}
.summary-item .s-value .tag{margin:2px 4px 2px 0}

/* Toast */
.toast{position:fixed;bottom:24px;right:24px;padding:14px 20px;background:#1e293b;color:#fff;border-radius:var(--radius);box-shadow:var(--shadow-lg);font-size:14px;z-index:1000;display:none;animation:slideUp 0.3s ease;max-width:400px}
.toast.show{display:flex;align-items:center;gap:10px}
.toast.success{border-left:4px solid var(--green)}
.toast.error{border-left:4px solid var(--red)}
@keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}

/* Loading */
.loading{display:flex;align-items:center;justify-content:center;padding:40px;color:var(--text3);gap:10px;font-size:14px}
.spinner{width:20px;height:20px;border:2px solid var(--bg4);border-top-color:var(--accent);border-radius:50%;animation:spin 0.6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* AI process log */
.ai-log-wrap{margin-top:10px;border:1px solid var(--bg4);border-radius:var(--radius-sm);overflow:hidden;background:#0d1117}
.ai-log-head{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--bg3);font-size:12px;font-weight:600;color:var(--text);border-bottom:1px solid var(--bg4)}
.ai-log{max-height:260px;overflow-y:auto;padding:10px 12px;font-family:var(--font-mono);font-size:11.5px;line-height:1.8;color:var(--text2)}
.ai-log-line{display:flex;align-items:flex-start;gap:8px;padding:1px 0}
.ai-log-line .ic{width:16px;text-align:center;flex-shrink:0;color:var(--accent);margin-top:1px}
.ai-log-line.done .ic{color:var(--green)}
.ai-log-line.active .msg{color:var(--text);font-weight:600}
.ai-log-line .msg{white-space:normal;word-break:break-word}
.ai-log-line .tag{display:inline-block;font-size:9px;text-transform:uppercase;letter-spacing:0.5px;padding:1px 6px;border-radius:4px;background:var(--bg3);color:var(--text3);margin-right:6px;flex-shrink:0;margin-top:2px}
.ai-log-line.done .tag{background:rgba(16,185,129,0.12);color:var(--green)}

/* Responsive */
@media(max-width:768px){
.form-row{grid-template-columns:1fr}
.module-grid{grid-template-columns:repeat(auto-fill,minmax(120px,1fr))}
.summary-grid{grid-template-columns:1fr}
.frontend-opts{grid-template-columns:1fr}
.prog-step .num{margin-right:0}
.prog-step .label{display:none}
.container{padding:20px 16px 80px}
}

/* Selected module tags */
.sel-modules{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.sel-module-tag{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;background:var(--accent-light);color:var(--accent);border-radius:20px;font-size:12px;font-weight:500}
.sel-module-tag .rm{padding:2px;cursor:pointer;border-radius:50%;width:16px;height:16px;display:flex;align-items:center;justify-content:center;font-size:8px;transition:var(--tr)}
.sel-module-tag .rm:hover{background:var(--accent);color:#fff}

/* ===== Module help modal (CSS / API reference) ===== */
.help-fab{position:fixed;right:22px;bottom:22px;z-index:2500;width:46px;height:46px;border-radius:50%;border:none;background:var(--accent);color:#fff;font-size:20px;font-weight:700;cursor:pointer;box-shadow:0 6px 20px rgba(99,102,241,0.45);display:flex;align-items:center;justify-content:center;transition:transform .15s, box-shadow .15s}
.help-fab:hover{transform:scale(1.08);box-shadow:0 8px 26px rgba(99,102,241,0.6)}
.help-modal{position:fixed;inset:0;z-index:2600;display:none;align-items:center;justify-content:center;background:rgba(15,23,42,0.6);backdrop-filter:blur(2px)}
.help-modal-overlay{position:absolute;inset:0}
.help-modal-content{position:relative;background:var(--bg2);border-radius:var(--radius);width:760px;max-width:94vw;max-height:86vh;display:flex;flex-direction:column;box-shadow:var(--shadow-lg);overflow:hidden}
.help-modal-header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border)}
.help-modal-header h3{font-size:16px;margin:0}
.help-close{background:none;border:none;color:var(--text3);font-size:22px;cursor:pointer;line-height:1}
.help-mod-filter{display:flex;gap:6px;flex-wrap:wrap;padding:12px 16px 4px}
.help-tabs{display:flex;gap:4px;padding:6px 16px 0;border-bottom:1px solid var(--border)}
.help-tab{padding:8px 16px;border:none;background:none;color:var(--text3);font-size:13px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent}
.help-tab:hover{color:var(--text1)}
.help-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.help-body{padding:14px 16px;overflow-y:auto;flex:1}
.ref-list{display:flex;flex-direction:column;gap:6px}
.ref-item{display:flex;align-items:center;gap:10px;padding:7px 10px;background:var(--bg3);border-radius:var(--radius-sm);font-size:12px}
.ref-sel{font-family:var(--font);color:var(--accent);font-weight:600;background:var(--accent-light);padding:2px 7px;border-radius:5px;white-space:nowrap}
.ref-kind{font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;text-transform:uppercase;color:#fff}
.ref-kind.id{background:#8b5cf6}
.ref-kind.class{background:#0ea5e9}
.ref-kind.attr{background:#64748b}
.ref-desc{color:var(--text2);flex:1}
.ref-notes{margin-top:12px;padding:10px 12px;background:var(--bg3);border-radius:var(--radius-sm);font-size:12px;line-height:1.55;color:var(--text2);white-space:pre-wrap}
.help-mini-q{background:none;border:1px solid var(--border);color:var(--text3);width:24px;height:24px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700;margin-left:8px}
.help-mini-q:hover{color:var(--accent);border-color:var(--accent)}
</style>
<script src="/static/js/i18n.js"></script>
</head>
<body>

<div class="topbar">
  <a href="/apps#extensions" class="back"><i class="fas fa-arrow-left"></i> Назад</a>
  <span class="logo"><i class="fas fa-cubes"></i> Extension Builder</span>
  <span class="subtitle">No-code конструктор расширений</span>
</div>

<div class="progress-wrap">
  <div class="progress" id="progressBar"></div>
</div>

<div class="container" id="app">
  <div id="editBanner" style="display:none;background:linear-gradient(135deg,rgba(99,102,241,0.15),rgba(168,85,247,0.15));border:1px solid var(--accent);border-radius:12px;padding:12px 16px;margin-bottom:14px;align-items:center;gap:12px">
    <i class="fas fa-pen-to-square" style="color:var(--accent);font-size:18px"></i>
    <div style="flex:1;font-size:13px;line-height:1.5">
      <b style="color:var(--accent)">Режим редактирования:</b> <span id="editBannerName" style="font-weight:600"></span><br>
      <span style="color:var(--text2)">Изменения сохраняются в существующее расширение без пересоздания. Кнопка на последнем шаге — «Сохранить изменения».</span>
    </div>
    <a href="/apps#extensions" class="btn btn-sm" style="padding:6px 14px;font-size:12px;white-space:nowrap"><i class="fas fa-arrow-left"></i> К списку</a>
  </div>
  <div id="jsErrorBanner" style="display:none;background:linear-gradient(135deg,rgba(239,68,68,0.15),rgba(220,38,38,0.1));border:1px solid #ef4444;border-radius:12px;padding:12px 16px;margin-bottom:14px;align-items:center;gap:12px">
    <i class="fas fa-exclamation-triangle" style="color:#ef4444;font-size:18px"></i>
    <div style="flex:1;font-size:13px;line-height:1.5">
      <b style="color:#ef4444">Ошибка JavaScript:</b> <span id="jsErrorBannerText" style="font-weight:600"></span><br>
      <span style="color:var(--text2)">Исправьте поле «JS» соответствующего элемента (или HTML/CSS) и нажмите «Сохранить изменения» ещё раз.</span>
    </div>
    <button class="btn btn-sm" style="padding:6px 14px;font-size:12px;white-space:nowrap" onclick="hideJsErrorBanner()"><i class="fas fa-times"></i> Закрыть</button>
  </div>
  <div class="loading" id="loadingScreen"><div class="spinner"></div> Загрузка данных...</div>
  <div id="stepContainer" style="display:none"></div>
</div>

<script>
const STEPS = [
  {icon:"fa-info-circle",label:"Основное"},
  {icon:"fa-puzzle-piece",label:"Модуль"},
  {icon:"fa-plug",label:"События"},
  {icon:"fa-book",label:"API"},
  {icon:"fa-lock",label:"Права"},
  {icon:"fa-cog",label:"Конфиг"},
  {icon:"fa-paint-brush",label:"Интерфейс"},
  {icon:"fa-globe",label:"Страницы"},
  {icon:"fa-rocket",label:"Генерация"}
];

let state = {
  name:"", version:"1.0.0", description:"", author:"", icon:"fa-puzzle-piece",
  target_module:"", target_modules:[],
  new_module:null,
  hooks:[],
  hook_actions:{},
  permissions:["storage"],
  config_schema:[],
  create_frontend:false,
  frontend_config:{button_text:"",button_icon:"fa-puzzle-piece",show_notification:false,add_styles:false,inject_html:"",show_badge:true},
  page_elements:[],
  dependencies:[], sandbox:false, auto_update:false, update_url:"",
  pinned_apis:[],
  custom_apis:[],
  module_pages:{}
};

const TEMPLATES = [
  {name:"minimal", label:"Минимальное", icon:"fa-cube", desc:"Простое расширение с логированием", config:{name:"my-extension", version:"1.0.0", description:"Простое расширение", author:"Автор", icon:"fa-puzzle-piece", target_module:"all", hooks:["on_load", "on_init"], hook_actions:{on_load:[{type:"log", params:{message:"Extension loaded", level:"info"}}], on_init:[{type:"log", params:{message:"Extension initialized", level:"info"}}]}, permissions:["storage"], config_schema:[], create_frontend:false, frontend_config:{}, dependencies:[], sandbox:false, auto_update:false, update_url:"", module_pages:{}}},
  {name:"search", label:"Поисковый", icon:"fa-search", desc:"Улучшает поиск: пред/пост-обработка", config:{name:"search-enhancer", version:"1.0.0", description:"Улучшает результаты поиска", author:"Автор", icon:"fa-search", target_module:"search", hooks:["before_search","after_search","result_render"], hook_actions:{before_search:[{type:"log", params:{message:"Search starting", level:"info"}}], after_search:[{type:"log", params:{message:"Search completed", level:"info"}},{type:"notify", params:{title:"Поиск завершён", message:"", type:"info", duration:"3000"}}], result_render:[{type:"modify_dom", params:{selector:".result-item", action:"addClass", value:"highlighted"}}]}, permissions:["storage","api:search"], config_schema:[], create_frontend:false, frontend_config:{}, dependencies:[], sandbox:false, auto_update:false, update_url:"", module_pages:{}}},
  {name:"ui-widget", label:"UI Виджет", icon:"fa-paint-brush", desc:"Добавляет кнопку/панель в интерфейс", config:{name:"ui-widget", version:"1.0.0", description:"Виджет для интерфейса", author:"Автор", icon:"fa-paint-brush", target_module:"ui", hooks:["nav_render","desktop_render"], hook_actions:{nav_render:[{type:"modify_dom", params:{selector:"#nav", action:"setHTML", value:'<button class="ext-btn">My Widget</button>'}}], desktop_render:[{type:"log", params:{message:"Desktop rendered", level:"info"}}]}, permissions:["storage","ui:modify"], config_schema:[{id:"widget_color", label:"Цвет виджета", type:"string", default:"#4361ee", description:""}], create_frontend:true, frontend_config:{button_text:"My Widget", button_icon:"fa-puzzle-piece", show_notification:true, add_styles:true, inject_html:"<div class='ext-widget'><p>Hello from extension!</p></div>"}, dependencies:[], sandbox:false, auto_update:false, update_url:"", module_pages:{}}},
  {name:"bookmark", label:"Закладки", icon:"fa-bookmark", desc:"Управление закладками в книгах", config:{name:"bookmark-manager", version:"1.0.0", description:"Управляет закладками в книгах", author:"Автор", icon:"fa-bookmark", target_module:"books", hooks:["book_open","bookmark_add","page_turn"], hook_actions:{book_open:[{type:"log", params:{message:"Book opened", level:"info"}},{type:"notify", params:{title:"Книга открыта", message:"", type:"info", duration:"2000"}}], bookmark_add:[{type:"api_call", params:{url:"/api/bookmarks", method:"POST", headers:'{"Content-Type":"application/json"}', body:'{"page": 1}'}}], page_turn:[{type:"log", params:{message:"Page turned", level:"debug"}}]}, permissions:["storage","api:books"], config_schema:[{id:"auto_bookmark", label:"Автозакладка", type:"boolean", default:"true", description:"Автоматически сохранять закладки"}], create_frontend:false, frontend_config:{}, dependencies:[], sandbox:false, auto_update:false, update_url:"", module_pages:{}}},
  {name:"api-connect", label:"API интеграция", icon:"fa-globe", desc:"Подключается к внешнему REST API", config:{name:"api-connector", version:"1.0.0", description:"Интеграция с внешним API", author:"Автор", icon:"fa-globe", target_module:"all", hooks:["on_init","load"], hook_actions:{on_init:[{type:"api_call", params:{url:"https://api.example.com/health", method:"GET", headers:"{}", body:""}}], load:[{type:"log", params:{message:"API Connector loaded", level:"info"}}]}, permissions:["storage","network"], config_schema:[{id:"api_url", label:"API URL", type:"string", default:"https://api.example.com", description:"Базовый URL API"},{id:"api_key", label:"API Key", type:"password", default:"", description:"Ключ доступа"}], create_frontend:false, frontend_config:{}, dependencies:["requests"], sandbox:false, auto_update:false, update_url:"", module_pages:{}}},
  {name:"custom", label:"Пустой шаблон", icon:"fa-file", desc:"Начните с нуля", config:{name:"", version:"1.0.0", description:"", author:"", icon:"fa-puzzle-piece", target_module:"all", hooks:[], hook_actions:{}, permissions:["storage"], config_schema:[], create_frontend:false, frontend_config:{}, dependencies:[], sandbox:false, auto_update:false, update_url:"", module_pages:{}}}
];

const SNIPPETS = [
  {id:"fetch_json", label:"Fetch JSON с API", desc:"Выполнить GET запрос и вернуть JSON", code:`import aiohttp
async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()`},
  {id:"debounce", label:"Debounce вызовов", desc:"Предотвращает частые вызовы функции", code:`import time
_last_call = {}
def debounce(key, cooldown=1.0):
    now = time.time()
    if key in _last_call and now - _last_call[key] < cooldown:
        return True
    _last_call[key] = now
    return False`},
  {id:"cache", label:"Кэш результатов", desc:"Кэширует результаты в памяти", code:`_cache = {}
def cached(key, ttl=60):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            now = time.time()
            if key in _cache and now - _cache[key]["time"] < ttl:
                return _cache[key]["data"]
            result = await func(*args, **kwargs)
            _cache[key] = {"data": result, "time": now}
            return result
        return wrapper
    return decorator`},
  {id:"file_storage", label:"Сохранение в файл", desc:"Сохраняет данные в JSON файл", code:`import json, os
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

def save_data(filename, data):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_data(filename, default=None):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)`},
  {id:"html_template", label:"HTML шаблон", desc:"Рендерит HTML с подстановкой переменных", code:`def render_template(template, **kwargs):
    for key, val in kwargs.items():
        template = template.replace("{{"+key+"}}", str(val))
    return template`},
  {id:"throttle", label:"Throttle вызовов", desc:"Ограничивает частоту вызовов", code:`import time
def throttle(min_interval=1.0):
    def decorator(func):
        last_called = [0.0]
        def wrapper(*args, **kwargs):
            now = time.time()
            if now - last_called[0] >= min_interval:
                last_called[0] = now
                return func(*args, **kwargs)
        return wrapper
    return decorator`}
];

const ACTION_TYPES = {
  log: {
    label: "Логирование",
    icon: "fa-code",
    desc: "Записать сообщение в лог",
    params: [
      {id:"message", label:"Сообщение", type:"text", default:"Hook called"},
      {id:"level", label:"Уровень", type:"select", default:"info", options:[{v:"info", l:"INFO"},{v:"warn", l:"WARN"},{v:"error", l:"ERROR"},{v:"debug", l:"DEBUG"}]}
    ]
  },
  notify: {
    label: "Уведомление",
    icon: "fa-bell",
    desc: "Показать уведомление пользователю",
    params: [
      {id:"title", label:"Заголовок", type:"text", default:"Расширение"},
      {id:"message", label:"Текст", type:"text", default:""},
      {id:"type", label:"Тип", type:"select", default:"info", options:[{v:"info", l:"Info"},{v:"success", l:"Success"},{v:"warning", l:"Warning"},{v:"error", l:"Error"}]},
      {id:"duration", label:"Длительность (мс)", type:"number", default:3000}
    ]
  },
  api_call: {
    label: "API запрос",
    icon: "fa-globe",
    desc: "Выполнить HTTP-запрос к API",
    params: [
      {id:"url", label:"URL", type:"text", default:""},
      {id:"method", label:"Метод", type:"select", default:"GET", options:[{v:"GET", l:"GET"},{v:"POST", l:"POST"},{v:"PUT", l:"PUT"},{v:"DELETE", l:"DELETE"}]},
      {id:"headers", label:"Заголовки (JSON)", type:"text", default:"{}"},
      {id:"body", label:"Тело запроса", type:"textarea", default:""}
    ]
  },
  modify_dom: {
    label: "Изменить DOM",
    icon: "fa-paint-brush",
    desc: "Изменить элемент на странице",
    params: [
      {id:"selector", label:"CSS селектор", type:"text", default:""},
      {id:"action", label:"Действие", type:"select", default:"show", options:[{v:"show", l:"Показать"},{v:"hide", l:"Скрыть"},{v:"toggle", l:"Переключить"},{v:"addClass", l:"Добавить класс"},{v:"removeClass", l:"Удалить класс"},{v:"setHTML", l:"Установить HTML"}]},
      {id:"value", label:"Значение", type:"text", default:""}
    ]
  },
  redirect: {
    label: "Перенаправление",
    icon: "fa-external-link-alt",
    desc: "Перейти по URL",
    params: [
      {id:"url", label:"URL", type:"text", default:""},
      {id:"target", label:"Куда", type:"select", default:"self", options:[{v:"self", l:"Текущее окно"},{v:"blank", l:"Новое окно"}]}
    ]
  },
  custom: {
    label: "Произвольный код",
    icon: "fa-terminal",
    desc: "Написать свой Python-код",
    params: [
      {id:"code", label:"Python код", type:"textarea", default:"# Your code here\npass"}
    ]
  }
};

let data = null;
let currentStep = 0;
let previewCode = {};

async function init(){
  try{
    const r = await fetch("/api/extensions/builder-data");
    data = await r.json();
    const editName = new URLSearchParams(window.location.search).get("edit");
    if(editName){
      state.editMode = editName;
      const ec = await fetch("/api/extensions/" + encodeURIComponent(editName) + "/builder-config");
      const ed = await ec.json();
      if(ed.config){
        state = Object.assign({}, state, ed.config);
        const banner = document.getElementById("editBanner");
        banner.style.display = "flex";
        document.getElementById("editBannerName").textContent = editName;
        if(ed.config.frontend_error){
          showJsErrorBanner("В расширении обнаружена ошибка JavaScript: " + ed.config.frontend_error);
        }
      } else {
        state.editMode = null;
      }
    }
    document.getElementById("loadingScreen").style.display = "none";
    document.getElementById("stepContainer").style.display = "block";
    renderProgress();
    showStep(0);
  }catch(e){
    document.getElementById("loadingScreen").innerHTML = "<i class='fas fa-exclamation-triangle' style='color:var(--red)'></i> Ошибка загрузки данных. Проверьте сервер.";
  }
}

function renderProgress(){
  const bar = document.getElementById("progressBar");
  bar.innerHTML = STEPS.map((s,i) =>
    `<div class="prog-step ${i===currentStep?'active':''}" data-step="${i}" onclick="showStep(${i})">
      <span class="num">${i+1}</span><span class="label">${s.label}</span>
    </div>`
  ).join("");
}

function showJsErrorBanner(msg){
  const b = document.getElementById("jsErrorBanner");
  if(!b) return;
  document.getElementById("jsErrorBannerText").textContent = msg || "";
  b.style.display = "flex";
}

function hideJsErrorBanner(){
  const b = document.getElementById("jsErrorBanner");
  if(b) b.style.display = "none";
}

function showStep(idx){
  if(idx < 0 || idx >= STEPS.length) return;
  currentStep = idx;
  document.querySelectorAll(".prog-step").forEach((el,i)=>{
    el.className = "prog-step" + (i===idx?" active":"") + (i<idx?" done":"");
  });
  renderStep(idx);
  window.scrollTo({top:0,behavior:'smooth'});
}

function renderStep(idx){
  const container = document.getElementById("stepContainer");
  switch(idx){
    case 0: container.innerHTML = renderMetadata(); break;
    case 1: container.innerHTML = renderModuleSelect(); break;
    case 2: container.innerHTML = renderHooks(); break;
    case 3: container.innerHTML = renderApiBrowser(); break;
    case 4: container.innerHTML = renderPermissions(); break;
    case 5: container.innerHTML = renderConfigSchema(); break;
    case 6: container.innerHTML = renderFrontend(); break;
    case 7: container.innerHTML = renderModulePages(); break;
    case 8:
      container.innerHTML = renderGenerate();
      setTimeout(initMonacoEditor, 50);
      break;
  }
}

/* ====== Step 0: Metadata ====== */
function renderMetadata(){
  return `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-info-circle" style="color:var(--accent)"></i> Основная информация</h2>
      <p>Заполните базовые данные вашего расширения: название, версию, описание. Название должно быть уникальным — оно используется как идентификатор. Описание увидят другие пользователи в магазине расширений.</p>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-tag"></i> О расширении</div>
      <div class="form-row">
        <div class="form-group">
          <label>Название расширения *</label>
          <input type="text" id="f_name" value="${escHtml(state.name)}" placeholder="my-awesome-extension" oninput="state.name=this.value;updatePreview();">
        </div>
        <div class="form-group">
          <label>Версия</label>
          <input type="text" id="f_version" value="${escHtml(state.version)}" placeholder="1.0.0" oninput="state.version=this.value;updatePreview();">
        </div>
      </div>
      <div class="form-group">
        <label>Описание</label>
        <textarea id="f_desc" placeholder="Краткое описание возможностей расширения" oninput="state.description=this.value;updatePreview();">${escHtml(state.description)}</textarea>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Автор</label>
          <input type="text" id="f_author" value="${escHtml(state.author)}" placeholder="Ваше имя" oninput="state.author=this.value;updatePreview();">
        </div>
        <div class="form-group">
          <label>Иконка</label>
          <div class="icon-grid" id="iconGrid">
            ${(data.icons||[]).map(ic =>
              `<div class="icon-opt ${state.icon===ic?'selected':''}" onclick="selectIcon('${ic}')"><i class="fas ${ic}"></i></div>`
            ).join("")}
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-magic"></i> AI-ассистент</div>
      <div id="aiSection">
        <p style="margin:0 0 10px;font-size:13px;color:var(--text2)">Опишите простыми словами на русском или английском, что должно делать ваше расширение. AI сам выберет модуль и хуки, напишет <b>Python-логику</b> и заполнит кодом вкладку <b>«Интерфейс»</b> (HTML, CSS, JS, Python). Например: <i>"расширение которое при открытии книги показывает уведомление с названием книги и добавляет на страницу панель с кнопкой сохранения в заметки"</i>.</p>
        <div style="display:flex;gap:8px">
          <textarea id="aiPrompt" placeholder="Например: расширение для книг — добавляет панель с закладками, кнопку «Сохранить в заметки» и показывает уведомление при открытии книги" style="flex:1;min-height:56px;padding:10px;border-radius:8px;border:1px solid var(--bg4);background:var(--bg2);color:var(--text1);font-size:13px;resize:vertical"></textarea>
          <button class="btn btn-primary" onclick="aiGenerate()" id="aiGenBtn" style="height:56px;white-space:nowrap"><i class="fas fa-wand-magic-sparkles"></i> Сгенерировать</button>
        </div>
        <div id="aiStatus" style="margin-top:8px;font-size:13px;display:none"></div>
        <div id="aiLogWrap" class="ai-log-wrap" style="display:none">
          <div class="ai-log-head"><i class="fas fa-robot" style="color:var(--accent)"></i> Процесс AI-генерации</div>
          <div id="aiLog" class="ai-log"></div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-template"></i> Шаблоны</div>
      <div class="template-grid">
        ${TEMPLATES.map(t => `
          <div class="template-card" onclick="applyTemplate('${t.name}')">
            <div class="tc-icon"><i class="fas ${t.icon}"></i></div>
            <div class="tc-name">${t.label}</div>
            <div class="tc-desc">${t.desc}</div>
          </div>
        `).join("")}
      </div>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-box"></i> Python-зависимости (pip)</div>
      <p style="font-size:12px;color:var(--text2);margin:0 0 8px">Укажите пакеты pip, которые нужны вашему расширению. По одному на строку. Например: <code>requests</code>, <code>beautifulsoup4</code></p>
      <textarea placeholder="requests&#10;beautifulsoup4&#10;numpy" oninput="state.dependencies=this.value.split('\n').map(s=>s.trim()).filter(Boolean)" style="width:100%;min-height:56px;padding:8px;border-radius:6px;border:1px solid var(--bg4);background:var(--bg2);color:var(--text1);font-size:12px;font-family:monospace;resize:vertical">${(state.dependencies||[]).join('\n')}</textarea>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-eye"></i> Предпросмотр</div>
      <div id="previewCard">
        ${buildPreviewCard()}
      </div>
    </div>
    ${navButtons(0)}
  </div>`;
}

function selectIcon(ic){
  state.icon = ic;
  document.querySelectorAll(".icon-opt").forEach(el=>el.classList.toggle("selected",el.querySelector("i").className.includes(ic.replace("fa-",""))));
}

function updatePreview(){
  const el = document.getElementById("previewCard");
  if(el) el.innerHTML = buildPreviewCard();
}

function buildPreviewCard(){
  return `
  <div class="preview-card">
    <div class="pv-icon"><i class="fas ${state.icon||'fa-puzzle-piece'}"></i></div>
    <div class="pv-info">
      <div class="pv-name">${escHtml(state.name||'my-extension')} <span class="ver">v${escHtml(state.version||'1.0.0')}</span></div>
      <div class="pv-author">${escHtml(state.author||'author')}</div>
      <div class="pv-desc">${escHtml(state.description||'Extension description')}</div>
    </div>
  </div>`;
}

/* ====== Step 1: Module Selection ====== */
function renderModuleSelect(){
  const modules = data.modules||[];
  const modInfo = data.module_info||{};
  return `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-puzzle-piece" style="color:var(--accent)"></i> Целевой модуль
        <button class="help-mini-q" onclick="openModuleHelp(getTargetModule())" title="Справочник модуля: CSS-селекторы и API">?</button>
      </h2>
      <p><b>Модуль</b> — это раздел программы: <b>Книги</b>, <b>Поиск</b>, <b>Настройки</b>, <b>Заметки</b> и т.д. Расширение привязывается к модулю и работает только на его страницах. Например: расширение для книг сработает, когда пользователь откроет книгу; расширение для поиска — при поиске.</p>
      <p>Выберите один или несколько модулей. Если хотите, чтобы расширение работало везде — выберите <b>«Все модули»</b>.</p>
      <div class="hint-box"><i class="fas fa-lightbulb"></i> <b>Хотите добавить кнопку или панель на страницу?</b> Выберите модуль, на страницах которого она появится. На шаге 6 (Интерфейс) вы сможете задать CSS-селектор, HTML-код и даже свои стили.</div>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-th-large"></i> Модули</div>
      <div class="module-grid">
        ${modules.map(m => `
          <div class="module-card ${state.target_modules.includes(m)?'selected':''} ${m==='all'?'special':''}"
               onclick="toggleModule('${m}')" title="${getModDesc(m)}">
            <span class="check"><i class="fas fa-check"></i></span>
            <span class="mod-icon"><i class="fas ${getModIcon(m)}"></i></span>
            <div class="mod-name">${modInfo[m]||m}</div>
            <div class="mod-desc">${getModDesc(m)}</div>
          </div>
        `).join("")}
        <div class="module-card special ${state.target_module==='__new__'?'selected':''}"
             onclick="showNewModuleForm()">
          <span class="check"><i class="fas fa-plus"></i></span>
          <span class="mod-icon"><i class="fas fa-plus-circle" style="color:var(--purple)"></i></span>
          <div class="mod-name">Новый модуль</div>
          <div class="mod-desc">Создать новый тип модуля</div>
        </div>
      </div>
      ${state.target_modules.length > 0 ? `
      <div class="sel-modules">
        ${state.target_modules.map(m => `
          <span class="sel-module-tag"><i class="fas ${getModIcon(m)}"></i> ${modInfo[m]||m} <span class="rm" onclick="toggleModule('${m}')">&times;</span></span>
        `).join("")}
      </div>` : `<div style="font-size:13px;color:var(--text3);margin-top:10px"><i class="fas fa-info-circle"></i> Выберите хотя бы один модуль или создайте новый</div>`}
    </div>
    <div class="new-module-form ${state.target_module==='__new__'?'show':''}" id="newModuleForm">
      <div class="card-title" style="color:var(--purple)"><i class="fas fa-plus-circle"></i> Создание нового модуля</div>
      <div class="hint-box" style="margin-bottom:12px;background:var(--bg3);border-color:var(--purple)">
        <i class="fas fa-info-circle" style="color:var(--purple)"></i>
        <b>Новый модуль</b> — это полностью новый раздел, которого раньше не было в программе. Вы создаёте его сами. После создания расширения в папке <code>custom_modules/</code> появится HTML-шаблон модуля. Вы сможете его отредактировать через менеджер файлов.
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Имя модуля *</label>
          <input type="text" id="nm_name" value="${state.new_module?.name||''}" placeholder="my-module" oninput="updateNewModule()">
          <div class="hint">Только латиница, без пробелов. Будет использоваться в URL</div>
        </div>
        <div class="form-group">
          <label>Отображаемое имя</label>
          <input type="text" id="nm_display" value="${state.new_module?.display_name||''}" placeholder="My Module" oninput="updateNewModule()">
          <div class="hint">Как модуль будет называться в меню</div>
        </div>
        <div class="form-group">
          <label>Описание модуля</label>
          <input type="text" id="nm_desc" value="${state.new_module?.description||''}" placeholder="Что делает этот модуль" oninput="updateNewModule()">
        </div>
      </div>
      <div style="margin-top:14px">
        <div style="font-size:13px;font-weight:600;color:var(--purple);margin-bottom:8px"><i class="fas fa-plug"></i> Хуки нового модуля</div>
        <p style="font-size:12px;color:var(--text3);margin-bottom:8px">Хуки (события) — это моменты, когда ваше расширение может что-то сделать. Например: "когда пользователь зашёл на страницу модуля", "когда нажата кнопка". Добавьте свои события.</p>
        <div id="customHooksList">
          ${(state.new_module?.hooks||[]).map((h,i) => `
            <div class="custom-hook-row">
              <input type="text" value="${escHtml(h.id)}" placeholder="hook_name" oninput="updateHookId(${i},this.value)">
              <input type="text" value="${escHtml(h.description)}" placeholder="Описание хука" oninput="updateHookDesc(${i},this.value)" style="flex:1.5">
              <button class="del-hook" onclick="removeHook(${i})"><i class="fas fa-times"></i></button>
            </div>
          `).join("")}
        </div>
        <button class="btn btn-ghost" onclick="addCustomHook()" style="font-size:12px"><i class="fas fa-plus"></i> Добавить хук</button>
      </div>
    </div>
    ${navButtons(1)}
  </div>`;
}

function getModIcon(m){
   const icons={agent:"fa-robot",books:"fa-book",media:"fa-music",notes:"fa-sticky-note",zim:"fa-archive",search:"fa-search",ui:"fa-paint-brush",desktop:"fa-window-restore",settings:"fa-cog",html:"fa-code",apps:"fa-th",avatar:"fa-user",education:"fa-graduation-cap",collection:"fa-folder",workspace:"fa-desktop",memory:"fa-brain","search-all":"fa-search-plus",article:"fa-file-alt",ask:"fa-question-circle",list:"fa-list",ai:"fa-brain",news:"fa-newspaper","task-manager":"fa-tasks",statistics:"fa-chart-bar",bookgen:"fa-book",files:"fa-folder-open",home:"fa-home",genlab:"fa-flask",browser:"fa-globe",social:"fa-users",qdrant:"fa-project-diagram",ha_panel:"fa-lightbulb",docker:"fa-cube",ai_engine:"fa-microchip",network:"fa-network-wired",all:"fa-globe"};
  return icons[m]||"fa-cube";
}

function getModDesc(m){
   const descs={agent:"AI агент и чат",books:"Книги и библиотека",media:"Медиафайлы",notes:"Заметки",zim:"ZIM-архивы",search:"Поиск",ui:"Интерфейс",desktop:"Рабочий стол",settings:"Настройки",html:"HTML-страницы",apps:"Приложения",avatar:"3D аватар",education:"Обучение",collection:"Коллекции",workspace:"Рабочее пространство",memory:"Память","search-all":"Поиск по всем источникам",article:"Статьи",ask:"Вопросы AI",list:"Список",ai:"AI ответы",news:"Новости","task-manager":"Менеджер задач",statistics:"Статистика",bookgen:"Генератор книг",files:"Файловый менеджер",home:"Главная страница",genlab:"GenLab (генерация медиа)",browser:"Браузер",social:"Соцсеть",qdrant:"Векторная база",ha_panel:"Умный дом",docker:"Docker",ai_engine:"AI Engine (админка)",network:"Сеть (админка)",all:"Глобально для всех"};
  return descs[m]||m;
}

function toggleModule(m){
  const idx = state.target_modules.indexOf(m);
  if(idx >= 0){
    state.target_modules.splice(idx,1);
  } else {
    // If "all" is selected, deselect others
    if(m === "all") state.target_modules = ["all"];
    else {
      state.target_modules = state.target_modules.filter(x => x !== "all");
      state.target_modules.push(m);
    }
  }
  if(state.target_module === "__new__" && state.target_modules.length > 0){
    // Deselect new module if a real module is selected
    state.target_module = "";
    document.getElementById("newModuleForm")?.classList.remove("show");
  }
  renderStep(1);
}

function showNewModuleForm(){
  state.target_module = "__new__";
  state.target_modules = [];
  if(!state.new_module) state.new_module = {name:"",display_name:"",description:"",hooks:[]};
  renderStep(1);
  setTimeout(()=>document.getElementById("newModuleForm")?.classList.add("show"),50);
}

function updateNewModule(){
  if(!state.new_module) state.new_module = {name:"",display_name:"",description:"",hooks:[]};
  state.new_module.name = document.getElementById("nm_name")?.value||"";
  state.new_module.display_name = document.getElementById("nm_display")?.value||"";
  state.new_module.description = document.getElementById("nm_desc")?.value||"";
}

function addCustomHook(){
  if(!state.new_module) state.new_module = {name:"",display_name:"",description:"",hooks:[]};
  state.new_module.hooks.push({id:"",description:""});
  renderStep(1);
}

function removeHook(idx){
  if(state.new_module) state.new_module.hooks.splice(idx,1);
  renderStep(1);
}

function updateHookId(idx,val){
  if(state.new_module) state.new_module.hooks[idx].id = val;
}

function updateHookDesc(idx,val){
  if(state.new_module) state.new_module.hooks[idx].description = val;
}

/* ====== Step 2: Hooks ====== */
function renderHooks(){
  const hooksData = data.hooks_by_module||{};
  const modules = state.target_modules.length > 0 ? state.target_modules :
    (state.target_module === "__new__" ? (state.new_module?.name ? [state.new_module.name] : []) : ["all"]);
  const modInfo = data.module_info||{};
  let html = `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-plug" style="color:var(--accent)"></i> Действия (Hook Actions)</h2>
      <p><b>Хуки (hooks)</b> — это события, которые происходят на <b>сервере</b>. Например: пользователь открыл книгу, отправил поисковый запрос, загрузилась страница. Вы можете назначить действия, которые выполнятся при наступлении этих событий.</p>
      <p style="font-size:12px;color:var(--text3);margin-top:4px">Выберите событие (хук) и добавьте к нему одно или несколько действий. Действия выполняются по порядку сверху вниз.</p>
      <div class="hint-box"><i class="fas fa-lightbulb"></i> <b>Хуки работают на сервере</b> — они не видны пользователю. Если хотите добавить кнопку, панель или изменить внешний вид страницы — используйте <b>Шаг 6 (Интерфейс) → «Добавить элемент на страницу»</b>. Там можно указать CSS-селектор, HTML, CSS, JavaScript и даже Python-код.</div>
    </div>`;
  if(modules.length === 0){
    html += `<div class="card"><div style="text-align:center;padding:20px;color:var(--text3)"><i class="fas fa-info-circle" style="font-size:24px;display:block;margin-bottom:8px"></i>Сначала выберите модуль на предыдущем шаге.</div></div>`;
  } else {
    modules.forEach(mod => {
      const hooks = hooksData[mod]||[];
      if(hooks.length === 0 && mod !== state.new_module?.name) return;
      html += `<div class="hooks-group">
        <div class="group-title"><i class="fas ${getModIcon(mod)}"></i> ${modInfo[mod]||mod}</div>`;
      hooks.forEach(h => {
        const checked = state.hooks.includes(h.id) ? "checked" : "";
        const actions = state.hook_actions[h.id] || [];
        html += `<div class="hook-item" style="flex-wrap:wrap;padding-bottom:${checked?'4':'8'}px">
          <label style="display:flex;align-items:center;gap:12px;flex:1;cursor:pointer;padding:4px 0">
            <input type="checkbox" ${checked} onchange="toggleHook('${h.id}',this.checked)" style="width:18px;height:18px;accent-color:var(--accent);cursor:pointer;flex-shrink:0">
            <span class="hook-label">${h.id}</span>
            <span class="hook-desc">${h.description||''}</span>
          </label>
          ${checked && actions.length > 0 ? `<div style="display:flex;gap:4px;flex-wrap:wrap">${actions.map(a => `<span class="action-badge ${a.type}"><i class="fas ${ACTION_TYPES[a.type]?.icon||'fa-code'}"></i> ${ACTION_TYPES[a.type]?.label||a.type}</span>`).join('')}</div>` : ''}
        </div>`;
        if(checked){
          html += renderActionArea(h.id);
        }
      });
      if(mod === state.new_module?.name && state.new_module?.hooks){
        state.new_module.hooks.forEach(h => {
          if(!h.id) return;
          const checked = state.hooks.includes(h.id) ? "checked" : "";
          const actions = state.hook_actions[h.id] || [];
          html += `<div class="hook-item" style="flex-wrap:wrap;padding-bottom:${checked?'4':'8'}px">
            <label style="display:flex;align-items:center;gap:12px;flex:1;cursor:pointer;padding:4px 0">
              <input type="checkbox" ${checked} onchange="toggleHook('${h.id}',this.checked)" style="width:18px;height:18px;accent-color:var(--accent);cursor:pointer;flex-shrink:0">
              <span class="hook-label">${h.id}</span>
              <span class="hook-desc">${h.description||''}</span>
            </label>
            ${checked && actions.length > 0 ? `<div style="display:flex;gap:4px;flex-wrap:wrap">${actions.map(a => `<span class="action-badge ${a.type}"><i class="fas ${ACTION_TYPES[a.type]?.icon||'fa-code'}"></i> ${ACTION_TYPES[a.type]?.label||a.type}</span>`).join('')}</div>` : ''}
          </div>`;
          if(checked){
            html += renderActionArea(h.id);
          }
        });
      }
      html += `</div>`;
    });
    const totalActions = Object.values(state.hook_actions).reduce((s,a) => s + a.length, 0);
    html += `<div class="card" style="background:var(--bg)">
      <div style="font-size:13px;color:var(--text3)">Выбрано: <strong style="color:var(--accent)">${state.hooks.length}</strong> хуков, <strong style="color:var(--green)">${totalActions}</strong> действий</div>
    </div>
    <div class="card" style="background:var(--bg2)">
      <div class="card-title"><i class="fas fa-question-circle"></i> Типы действий</div>
      <div style="font-size:12px;color:var(--text2);display:grid;grid-template-columns:1fr 1fr;gap:6px">
        ${Object.entries(ACTION_TYPES).map(([k,v]) =>
          `<div style="padding:6px 8px;background:var(--bg3);border-radius:6px"><b><i class="fas ${v.icon}"></i> ${v.label}</b>: ${v.desc}</div>`
        ).join("")}
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--text3)">Действия выполняются по порядку. Если их несколько — сначала выполнится первое, потом второе и т.д.</div>
    </div>`;
  }
  html += navButtons(2);
  return html + `</div>`;
}

function renderActionArea(hookId){
  const actions = state.hook_actions[hookId] || [];
  let html = `<div class="action-area" id="actionArea_${hookId}">`;
  actions.forEach((a, idx) => {
    html += renderActionCard(hookId, idx, a);
  });
  html += `<button class="add-action-btn" onclick="addAction('${hookId}')"><i class="fas fa-plus"></i> Добавить действие</button>`;
  html += `</div>`;
  return html;
}

function renderActionCard(hookId, idx, action){
  const t = ACTION_TYPES[action.type] || ACTION_TYPES.log;
  const actions = state.hook_actions[hookId] || [];
  const total = actions.length;
  let paramsHtml = '';
  t.params.forEach(p => {
    const val = action.params?.[p.id] !== undefined ? action.params[p.id] : (p.default||'');
    const cls = p.type === 'textarea' ? 'full-w' : '';
    if(p.type === 'select'){
      const opts = (p.options||[]).map(o =>
        `<option value="${o.v}" ${val===o.v?'selected':''}>${o.l}</option>`
      ).join('');
      paramsHtml += `<div class="${cls}"><label>${p.label}</label><select onchange="updateActionParam('${hookId}',${idx},'${p.id}',this.value)">${opts}</select></div>`;
    } else if(p.type === 'textarea'){
      const taId = `actionParam_${hookId}_${idx}_${p.id}`;
      paramsHtml += `<div class="${cls}"><label>${p.label}${action.type==='custom' ? ' <button class="btn btn-ghost" onclick="showSnippetBrowser(\''+taId+'\')" style="font-size:10px;padding:1px 6px;border-radius:4px"><i class="fas fa-code"></i> Сниппеты</button>' : ''}</label><textarea id="${taId}" onchange="updateActionParam('${hookId}',${idx},'${p.id}',this.value)">${escHtml(val)}</textarea></div>`;
    } else {
      paramsHtml += `<div class="${cls}"><label>${p.label}</label><input type="${p.type}" value="${escHtml(val)}" onchange="updateActionParam('${hookId}',${idx},'${p.id}',this.value)"></div>`;
    }
  });
  // Condition field
  const condVal = action.condition || '';
  const condHtml = `<div class="full-w" style="margin-top:4px"><label><i class="fas fa-code-branch" style="font-size:10px;color:var(--text3)"></i> Условие (оставьте пустым для безусловного)</label><input type="text" placeholder="kwargs.get('data') and len(kwargs.get('data')) > 0" value="${escHtml(condVal)}" onchange="updateActionCondition('${hookId}',${idx},this.value)" style="font-size:11px;font-family:monospace"></div>`;
  return `
    <div class="action-card">
      <div class="ac-head">
        <i class="fas ${t.icon}" style="color:var(--accent);font-size:14px"></i>
        <select onchange="changeActionType('${hookId}',${idx},this.value)" style="font-size:12px">
          ${Object.entries(ACTION_TYPES).map(([k,v]) =>
            `<option value="${k}" ${action.type===k?'selected':''}>${v.label}</option>`
          ).join('')}
        </select>
        <div style="display:flex;gap:2px">
          ${idx > 0 ? `<button class="move-btn" onclick="moveAction('${hookId}',${idx},-1)" title="Вверх"><i class="fas fa-chevron-up"></i></button>` : ''}
          ${idx < total-1 ? `<button class="move-btn" onclick="moveAction('${hookId}',${idx},1)" title="Вниз"><i class="fas fa-chevron-down"></i></button>` : ''}
        </div>
        <button class="del-action" onclick="removeAction('${hookId}',${idx})" title="Удалить действие"><i class="fas fa-trash"></i></button>
      </div>
      <div class="ac-params">${paramsHtml}</div>
      ${condHtml}
    </div>`;
}

function toggleHook(hookId, checked){
  if(checked){
    if(!state.hooks.includes(hookId)){
      state.hooks.push(hookId);
      if(!state.hook_actions[hookId]){
        state.hook_actions[hookId] = [{type:"log", params:{message:`${hookId} called`, level:"info"}}];
      }
    }
  } else {
    state.hooks = state.hooks.filter(h => h !== hookId);
  }
  renderStep(2);
}

function addAction(hookId){
  if(!state.hook_actions[hookId]) state.hook_actions[hookId] = [];
  const t = Object.keys(ACTION_TYPES)[0]; // "log"
  const params = {};
  ACTION_TYPES[t].params.forEach(p => { params[p.id] = p.default||''; });
  state.hook_actions[hookId].push({type:t, params});
  renderStep(2);
}

function removeAction(hookId, idx){
  state.hook_actions[hookId].splice(idx,1);
  if(state.hook_actions[hookId].length === 0){
    delete state.hook_actions[hookId];
  }
  renderStep(2);
}

function changeActionType(hookId, idx, newType){
  const action = state.hook_actions[hookId][idx];
  action.type = newType;
  const t = ACTION_TYPES[newType];
  const newParams = {};
  t.params.forEach(p => { newParams[p.id] = action.params?.[p.id] !== undefined ? action.params[p.id] : (p.default||''); });
  action.params = newParams;
  renderStep(2);
}

function updateActionParam(hookId, idx, paramId, value){
  if(state.hook_actions[hookId] && state.hook_actions[hookId][idx]){
    state.hook_actions[hookId][idx].params[paramId] = value;
  }
}

function moveAction(hookId, idx, direction){
  const arr = state.hook_actions[hookId];
  if(!arr) return;
  const newIdx = idx + direction;
  if(newIdx < 0 || newIdx >= arr.length) return;
  const tmp = arr[idx];
  arr[idx] = arr[newIdx];
  arr[newIdx] = tmp;
  renderStep(2);
}

function updateActionCondition(hookId, idx, value){
  if(state.hook_actions[hookId] && state.hook_actions[hookId][idx]){
    state.hook_actions[hookId][idx].condition = value;
  }
}

/* ====== Templates & Snippets ====== */
function applyTemplate(name){
  const t = TEMPLATES.find(t => t.name === name);
  if(!t) return;
  const cfg = JSON.parse(JSON.stringify(t.config)); // deep clone
  if(!state.name) state.name = cfg.name;
  state.version = cfg.version;
  if(!state.description) state.description = cfg.description;
  state.author = cfg.author || state.author;
  state.icon = cfg.icon || state.icon;
  state.target_module = cfg.target_module;
  state.target_modules = cfg.target_module === "all" ? ["all"] : [cfg.target_module];
  state.hooks = cfg.hooks || [];
  state.hook_actions = cfg.hook_actions || {};
  state.permissions = cfg.permissions || ["storage"];
  state.config_schema = cfg.config_schema || [];
  state.frontend_config = cfg.frontend_config || {};
  state.dependencies = cfg.dependencies || [];
  state.sandbox = !!cfg.sandbox;
  state.auto_update = !!cfg.auto_update;
  state.update_url = cfg.update_url || "";
  state.pinned_apis = state.pinned_apis || [];
  state.custom_apis = state.custom_apis || [];
  renderStep(0);
  showToast("Шаблон \"" + t.label + "\" применён!", "success");
}

function showSnippetBrowser(textareaId){
  const existing = document.querySelector(".snip-modal");
  if(existing) existing.remove();
  const m = document.createElement("div");
  m.className = "snip-modal";
  m.innerHTML = `
    <div class="snip-modal-content">
      <div class="snip-modal-header">
        <h3><i class="fas fa-code"></i> Библиотека сниппетов</h3>
        <button onclick="this.closest('.snip-modal').remove()">&times;</button>
      </div>
      <div class="snip-list">
        ${SNIPPETS.map(s => `
          <div class="snip-item" onclick="insertSnippet('${textareaId}','${escHtml(s.code)}');this.closest('.snip-modal').remove()">
            <div class="snip-label"><i class="fas fa-file-code" style="color:var(--accent);margin-right:6px"></i>${s.label}</div>
            <div class="snip-desc">${s.desc}</div>
            <pre class="snip-code">${escHtml(s.code)}</pre>
          </div>
        `).join("")}
      </div>
    </div>`;
  document.body.appendChild(m);
}

function insertSnippet(textareaId, code){
  const ta = document.getElementById(textareaId);
  if(!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  ta.value = ta.value.substring(0, start) + code + ta.value.substring(end);
  ta.selectionStart = ta.selectionEnd = start + code.length;
  ta.dispatchEvent(new Event("change"));
  // Also update state directly
  const hookActions = state.hook_actions;
  // Find which custom action textarea this is by looking at parent
}

/* ====== Step 3: API Browser ====== */
async function refreshApiData(silent){
  try{
    if(!silent){
      document.getElementById("apiSearch")?.style.setProperty("background-image","none");
    }
    await fetch("/api/extensions/builder-data/refresh", {method:"POST"});
    const r = await fetch("/api/extensions/builder-data");
    const newData = await r.json();
    if(newData && newData.api_routes){
      data = newData;
      if(document.getElementById("stepContainer")?.querySelector(".api-list")){
        const prevPinned = state.pinned_apis || [];
        renderStep(3);
        state.pinned_apis = prevPinned;
      }
      if(!silent) showToast("API обновлён: "+newData.api_source, "success");
    }
  }catch(e){
    if(!silent) showToast("Ошибка обновления: "+e.message, "error");
  }
}

function renderApiBrowser(){
  const routes = data.api_routes||[];
  const loadError = data.api_load_error || "";
  const currentMod = getTargetModule();
  const allModules = ["all","agent","books","media","notes","search","ui","desktop","settings","apps","avatar","education","workspace","memory"];

  // Group routes by prefix
  const grouped = {};
  routes.forEach(r => {
    const prefix = r.path.split("/").slice(0,3).join("/") || "/";
    if(!grouped[prefix]) grouped[prefix] = [];
    grouped[prefix].push(r);
  });

  const prefixToModule = {
    "/api/extensions": "all", "/api/agent": "agent", "/api/books": "books",
    "/api/media": "media", "/api/notes": "notes", "/api/search": "search",
    "/api/ui": "ui", "/api/settings": "settings", "/api/apps": "apps",
    "/api/avatar": "avatar", "/api/education": "education",
    "/api/workspace": "workspace", "/api/memory": "memory",
    "/api/chat": "agent", "/api/ai": "agent", "/api/llm": "agent",
    "/api/collection": "books", "/api/zim": "books", "/api/library": "books",
    "/api/brain": "memory", "/api/knowledge": "memory",
    "/api/image": "media", "/api/photos": "media", "/api/video": "media",
    "/api/html": "html", "/api/config": "settings", "/api/files": "settings"
  };

  let html = `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-book" style="color:var(--accent)"></i> Документация API</h2>
      <p>Просмотрите эндпоинты сервера. Нажмите <i class="fas fa-thumbtack" style="color:var(--accent)"></i> чтобы прикрепить нужный — он попадёт в код расширения как комментарий. Это нужно, если расширение делает HTTP-запросы к серверу (через действие <b>API запрос</b> на шаге 2).</p>
      <div class="hint-box"><i class="fas fa-info-circle"></i> <b>Как это работает:</b> Кнопки модулей сверху (<span class="api-mod-filter-btn" style="display:inline;padding:1px 6px;font-size:11px">Все</span>, <span class="api-mod-filter-btn" style="display:inline;padding:1px 6px;font-size:11px">AI</span>, …) фильтруют эндпоинты по категориям. Если категория пуста — API для этого модуля не обнаружены при сканировании, но вы можете добавить любой эндпоинт вручную через кнопку «Добавить эндпоинт» внизу. <b>Прикреплённые</b> эндпоинты (<i class="fas fa-thumbtack" style="color:var(--accent)"></i>) будут включены в код расширения как ссылки на документацию. Значок <span class="curated-badge" style="display:inline;padding:0 4px;font-size:10px">👍</span> означает, что эндпоинт проверен и имеет описание.</div>
    </div>

    <div class="card">
      <div class="api-search">
        <i class="fas fa-search" style="color:var(--text3)"></i>
        <input type="text" id="apiSearch" placeholder="Поиск эндпоинтов..." oninput="filterApis()" autocomplete="off">
        <span style="font-size:12px;color:var(--text3)"><span id="apiCount">${routes.length}</span> всего</span>
        <button class="btn btn-ghost btn-sm" onclick="refreshApiData()" title="Обновить список API" style="margin-left:6px;font-size:11px"><i class="fas fa-sync-alt"></i></button>
      </div>

      <div class="api-module-filter" id="apiModFilter">
        <button class="api-mod-filter-btn active" data-mod="" onclick="setApiModFilter('',this)">Все</button>
        ${allModules.map(m => {
          const label = (data.module_info||{})[m] || m;
          const active = currentMod === m ? 'active' : '';
          return `<button class="api-mod-filter-btn ${active}" data-mod="${m}" onclick="setApiModFilter('${m}',this)">${label}</button>`;
        }).join("")}
      </div>

      <div class="api-list" id="apiList">
        ${routes.length === 0 ? `
        <div class="api-empty-state">
          <i class="fas fa-cloud big-icon"></i>
          <h4>Нет данных об API</h4>
          <p>${loadError ? 'Ошибка загрузки: ' + escHtml(loadError) : 'Сервер API не обнаружен. Возможно, dev-сервер не запущен.'}<br>Вы можете добавить эндпоинты вручную или продолжить позже.</p>
          <button class="btn btn-ghost" onclick="toggleManualApiForm()"><i class="fas fa-plus"></i> Добавить эндпоинт вручную</button>
        </div>
        ` : Object.entries(grouped).map(([prefix, rs]) => {
          const mod = prefixToModule[prefix] || "all";
          const showByDefault = !currentMod || currentMod === "all" || currentMod === mod;
          return `
          <div class="api-group" data-prefix="${prefix}" data-mod="${mod}" style="display:${showByDefault?'block':'none'}">
            <div class="api-opt-group"><i class="fas ${getModIcon(mod)}" style="margin-right:6px"></i> ${(data.module_info||{})[mod]||mod} — ${prefix}</div>
            ${rs.map(r => {
              const isPinned = state.pinned_apis.includes(r.path);
              const isCurated = r.curated;
              return `<div class="api-item" data-path="${r.path}" data-method="${r.method}">
                <span class="method-tag ${r.method.toLowerCase()}">${r.method}</span>
                <span class="api-path">${r.path}</span>
                <span class="api-name">${escHtml(r.description||r.name||r.endpoint||'')}${isCurated?'<span class="curated-badge">common</span>':''}</span>
                <button class="api-copy" onclick="copyApiPath('${r.path}')" title="Копировать путь"><i class="fas fa-copy"></i></button>
                <span class="api-pin ${isPinned?'pinned':''}" onclick="toggleApiPin('${r.path}')" title="${isPinned?'Открепить':'Запомнить'} эндпоинт"><i class="fas fa-thumbtack"></i></span>
              </div>`;
            }).join("")}
          </div>`;
        }).join("")}
      </div>

      <div class="api-manual-form" id="apiManualForm">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px"><i class="fas fa-plus-circle"></i> Добавить эндпоинт вручную</div>
        <div class="form-row">
          <select id="apiManualMethod" style="max-width:90px">
            <option value="GET">GET</option><option value="POST">POST</option>
            <option value="PUT">PUT</option><option value="DELETE">DELETE</option>
          </select>
          <input type="text" id="apiManualPath" placeholder="/api/example/endpoint" style="font-family:monospace">
          <input type="text" id="apiManualName" placeholder="Описание (необязательно)">
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-primary" onclick="addCustomApi()" style="font-size:12px;padding:6px 16px"><i class="fas fa-check"></i> Добавить</button>
          <button class="btn btn-ghost" onclick="toggleManualApiForm()" style="font-size:12px">Отмена</button>
        </div>
      </div>

      <button class="btn btn-ghost" onclick="toggleManualApiForm()" style="margin-top:6px;font-size:12px" id="apiManualToggle"><i class="fas fa-plus"></i> Добавить эндпоинт вручную</button>
    </div>

    ${state.pinned_apis.length > 0 || state.custom_apis.length > 0 ? `
    <div class="card">
      <div class="card-title"><i class="fas fa-thumbtack"></i> Отмеченные эндпоинты (${state.pinned_apis.length + state.custom_apis.length})</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">
        ${state.pinned_apis.map(p => `<span class="badge badge-module" style="cursor:pointer" onclick="toggleApiPin('${p}')" title="Убрать"><i class="fas fa-thumbtack"></i> ${p} <span style="margin-left:4px;color:var(--red)">&times;</span></span>`).join("")}
        ${state.custom_apis.map((a,i) => `<span class="badge badge-module" style="background:var(--bg3);cursor:pointer" onclick="removeCustomApi(${i})" title="Удалить"><span class="method-tag ${a.method.toLowerCase()}" style="font-size:9px;padding:1px 5px">${a.method}</span> ${a.path} <span style="margin-left:4px;color:var(--red)">&times;</span></span>`).join("")}
      </div>
    </div>` : ""}
    ${navButtons(3)}
  </div>`;
  return html;
}

function toggleApiPin(path){
  const idx = state.pinned_apis.indexOf(path);
  if(idx >= 0) state.pinned_apis.splice(idx,1);
  else state.pinned_apis.push(path);
  renderStep(3);
}

function filterApis(){
  const q = document.getElementById("apiSearch")?.value?.toLowerCase()||"";
  document.querySelectorAll(".api-item").forEach(el => {
    const text = (el.dataset.path + " " + el.dataset.method + " " + el.textContent).toLowerCase();
    const matches = text.includes(q);
    el.style.display = matches ? "flex" : "none";
  });
  // Update visible group counts
  document.querySelectorAll(".api-group").forEach(g => {
    const visible = [...g.querySelectorAll(".api-item")].filter(e => e.style.display !== "none").length;
    const header = g.querySelector(".api-opt-group");
    if(header) header.textContent = header.textContent.replace(/\([0-9]+\)/g, "") + ` (${visible})`;
  });
  // Update total count
  const visibleTotal = document.querySelectorAll(".api-item:not([style*='display: none'])").length;
  const countEl = document.getElementById("apiCount");
  if(countEl) countEl.textContent = visibleTotal;
}

let _apiModFilter = "";
function setApiModFilter(mod, btn){
  _apiModFilter = mod;
  document.querySelectorAll(".api-mod-filter-btn").forEach(b => b.classList.remove("active"));
  if(btn) btn.classList.add("active");
  document.querySelectorAll(".api-group").forEach(g => {
    const gMod = g.dataset.mod || "all";
    g.style.display = (!mod || mod === "all" || gMod === mod) ? "block" : "none";
  });
  filterApis();
}

function toggleManualApiForm(){
  const form = document.getElementById("apiManualForm");
  const toggle = document.getElementById("apiManualToggle");
  if(!form) return;
  const show = form.style.display !== "block";
  form.style.display = show ? "block" : "none";
  if(toggle) toggle.style.display = show ? "none" : "inline-flex";
}

function addCustomApi(){
  const method = document.getElementById("apiManualMethod")?.value || "GET";
  const path = document.getElementById("apiManualPath")?.value?.trim();
  const name = document.getElementById("apiManualName")?.value?.trim();
  if(!path){ showToast("Укажите путь эндпоинта", "error"); return; }
  state.custom_apis.push({method, path, name});
  state.pinned_apis.push(path); // auto-pin
  document.getElementById("apiManualPath").value = "";
  document.getElementById("apiManualName").value = "";
  toggleManualApiForm();
  renderStep(3);
  showToast("Эндпоинт добавлен!", "success");
}

function removeCustomApi(idx){
  const api = state.custom_apis[idx];
  if(api){
    const pi = state.pinned_apis.indexOf(api.path);
    if(pi >= 0) state.pinned_apis.splice(pi,1);
    state.custom_apis.splice(idx,1);
  }
  renderStep(3);
}

function copyApiPath(path){
  navigator.clipboard?.writeText(path).then(() => {
    showToast("Путь скопирован: " + path, "success");
  }).catch(() => {
    showToast("Путь: " + path, "info");
  });
}

/* ====== Step 4: Permissions ====== */
function renderPermissions(){
  const perms = data.permissions||[];
  return `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-lock" style="color:var(--accent)"></i> Разрешения</h2>
      <p>Определите, к каким ресурсам системы будет иметь доступ ваше расширение. Без нужного разрешения расширение не сможет выполнять соответствующие действия.</p>
      <div class="hint-box"><i class="fas fa-info-circle"></i> <b>storage</b> — сохранять данные расширения (настройки, файлы); <b>network</b> — делать запросы к внешним сайтам; <b>ui:modify</b> — изменять интерфейс (CSS/DOM); <b>api:books, api:search, api:agent</b> — доступ к API конкретного модуля; <b>shell</b> — выполнять команды на сервере (опасно!). Выбирайте только те разрешения, которые действительно нужны вашему расширению.</div>
    </div>
    <div class="card">
      ${perms.map(p => `
        <div class="perm-item" onclick="togglePerm('${p.id}')">
          <div class="toggle ${state.permissions.includes(p.id)?'active':''}" id="perm_${p.id}"></div>
          <div class="perm-info">
            <div class="perm-name">${p.id}</div>
            <div class="perm-desc">${p.description}</div>
          </div>
        </div>
      `).join("")}
    </div>
    ${navButtons(4)}
  </div>`;
}

function togglePerm(id){
  const idx = state.permissions.indexOf(id);
  if(idx >= 0) state.permissions.splice(idx,1);
  else state.permissions.push(id);
  const el = document.getElementById("perm_"+id);
  if(el) el.classList.toggle("active");
}

/* ====== Step 5: Config Schema ====== */
function renderConfigSchema(){
  return `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-cog" style="color:var(--accent)"></i> Конфигурация расширения</h2>
      <p>Добавьте поля, которые пользователь сможет настраивать в окне настроек расширения. Например: API-ключ, цвет темы, выбор режима работы.</p>
      <div class="hint-box"><i class="fas fa-info-circle"></i> <b>Типы полей:</b> <b>string</b> — текст, <b>number</b> — число, <b>boolean</b> — вкл/выкл, <b>select</b> — выбор из списка, <b>password</b> — скрытое поле для паролей/ключей. Если расширению не нужны настройки — просто нажмите «Далее».</div>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-list"></i> Поля конфигурации</div>
      <div id="configFields">
        ${state.config_schema.length === 0 ? '<div style="font-size:13px;color:var(--text3);padding:8px 0">Пока нет полей. Нажмите "Добавить поле" ниже.</div>' :
          state.config_schema.map((f,i) => `
            <div class="config-field">
              <i class="fas fa-grip-vertical" style="color:var(--text3);cursor:grab"></i>
              <input type="text" value="${escHtml(f.id)}" placeholder="Имя поля" oninput="updateConfigField(${i},'id',this.value)">
              <input type="text" value="${escHtml(f.label)}" placeholder="Метка" oninput="updateConfigField(${i},'label',this.value)">
              <select onchange="updateConfigField(${i},'type',this.value)">
                <option value="string" ${f.type==='string'?'selected':''}>string</option>
                <option value="number" ${f.type==='number'?'selected':''}>number</option>
                <option value="boolean" ${f.type==='boolean'?'selected':''}>boolean</option>
                <option value="select" ${f.type==='select'?'selected':''}>select</option>
                <option value="password" ${f.type==='password'?'selected':''}>password</option>
              </select>
              <input type="text" value="${escHtml(f.default)}" placeholder="По умолч." oninput="updateConfigField(${i},'default',this.value)" style="max-width:100px">
              <input type="text" value="${escHtml(f.description)}" placeholder="Описание" oninput="updateConfigField(${i},'description',this.value)">
              <button class="del-field" onclick="removeConfigField(${i})"><i class="fas fa-trash-alt"></i></button>
            </div>
          `).join("")}
      </div>
      <button class="btn btn-ghost" onclick="addConfigField()" style="margin-top:6px"><i class="fas fa-plus"></i> Добавить поле</button>
    </div>
    ${navButtons(5)}
  </div>`;
}

function addConfigField(){
  state.config_schema.push({id:"",label:"",type:"string",default:"",description:""});
  renderStep(5);
}

function removeConfigField(idx){
  state.config_schema.splice(idx,1);
  renderStep(5);
}

function updateConfigField(idx,key,val){
  if(state.config_schema[idx]) state.config_schema[idx][key] = val;
}

/* ====== Step 6: Frontend ====== */
function renderFrontend(){
  return `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-paint-brush" style="color:var(--accent)"></i> Интерфейс — Frontend.js</h2>
      <p>Настройте, как расширение будет выглядеть и работать в браузере пользователя. Включите <b>«Создать frontend.js»</b>, если хотите добавлять кнопки, панели, менять стили или вставлять HTML на страницу.</p>
      <p style="font-size:12px;color:var(--text3);margin-top:4px">Без frontend.js расширение будет работать только на сервере (хуки, Python, API-запросы). Включите его, если расширение должно что-то показывать пользователю.</p>
      <div class="hint-box"><i class="fas fa-lightbulb"></i> <b>«Добавить элемент на страницу»</b> — вставьте произвольный HTML на страницу модуля по CSS-селектору. Можно добавить несколько элементов — каждый со своим HTML, CSS, JS или Python.<br>
      <b>Примеры селекторов:</b> <code>.module-header</code> (класс), <code>#chat-area</code> (id), <code>nav</code> (тег), <code>.sidebar &gt; .top</code> (вложенный).
      <b>Размещение:</b> «Внутрь (в конец)» — ваш HTML добавится последним дочерним, «Перед элементом» — сразу перед селектором и т.д.
      В полях <b>CSS</b> и <b>JavaScript</b> можно задать стили и скрипты для нового элемента.
      Поле <b>Python</b> — серверная функция, которая будет доступна при взаимодействии с элементом.</div>
    </div>
    <div class="card">
      <label class="frontend-opt" style="margin-bottom:12px;cursor:pointer">
        <input type="checkbox" ${state.create_frontend?'checked':''} onchange="state.create_frontend=this.checked;renderStep(6)">
        <span style="font-weight:600">Создать frontend.js</span>
        <span style="font-size:12px;color:var(--text3);margin-left:auto">JavaScript для интерфейса</span>
      </label>
      ${state.create_frontend ? `
      <div class="form-row" style="margin-bottom:12px">
        <div class="form-group">
          <label>Текст кнопки</label>
          <input type="text" value="${escHtml(state.frontend_config.button_text)}" placeholder="Моя кнопка" oninput="state.frontend_config.button_text=this.value">
        </div>
        <div class="form-group">
          <label>Иконка кнопки</label>
          <select onchange="state.frontend_config.button_icon=this.value">
            ${(data.icons||[]).map(ic =>
              `<option value="${ic}" ${state.frontend_config.button_icon===ic?'selected':''}>${ic}</option>`
            ).join("")}
          </select>
        </div>
      </div>
      <div class="frontend-opts">
        <label class="frontend-opt">
          <input type="checkbox" ${state.frontend_config.show_notification?'checked':''} onchange="state.frontend_config.show_notification=this.checked">
          <span>Показывать уведомление при клике</span>
        </label>
        <label class="frontend-opt">
          <input type="checkbox" ${state.frontend_config.add_styles?'checked':''} onchange="state.frontend_config.add_styles=this.checked">
          <span>Добавить CSS-стили</span>
        </label>
        <label class="frontend-opt">
          <input type="checkbox" ${state.frontend_config.show_badge!==false?'checked':''} onchange="state.frontend_config.show_badge=this.checked">
          <span>Показывать значок расширения в панели</span>
        </label>
      </div>
      <div class="form-group" style="margin-top:12px">
        <label>Дополнительный HTML-код для вставки</label>
        <textarea placeholder="<div>Привет, мир!</div>" oninput="state.frontend_config.inject_html=this.value">${escHtml(state.frontend_config.inject_html)}</textarea>
      </div>` : ""}

      <div class="card" style="margin-top:16px">
        <div class="card-title"><i class="fas fa-plus-circle" style="color:var(--accent)"></i> Добавить элемент на страницу</div>
        <p style="font-size:13px;color:var(--text3);margin-bottom:12px">
          Укажите <b>CSS-селектор</b> (класс, id или тег) — туда будет вставлен ваш HTML. Если оставить селектор пустым, HTML добавится в конец страницы.
          Каждое поле (HTML, CSS, JS, Python) работает независимо — можно заполнить только CSS (изменить стили элемента), только JS (показать alert/изменить поведение) или всё вместе.
          Элемент будет добавлен автоматически при загрузке страницы модуля.
        </p>
        <div id="pageElementsList">
          ${state.page_elements.length === 0 ? '<div style="padding:12px;text-align:center;color:var(--text3);font-style:italic">Нет элементов. Нажмите «Добавить элемент».</div>' : ''}
          ${state.page_elements.map((el,i) => `
          <div class="pe-item" data-idx="${i}" style="border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px;background:var(--bg2)">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
              <input type="text" value="${escHtml(el.selector||'')}" oninput="updatePageElement(${i},'selector',this.value)" placeholder="CSS-селектор: .page-header, #toolbar, nav" style="flex:2;min-width:180px;font-family:monospace;font-size:12px">
              <select onchange="updatePageElement(${i},'placement',this.value)" style="width:140px">
                <option value="append" ${el.placement==='append'?'selected':''}>Внутрь (в конец)</option>
                <option value="prepend" ${el.placement==='prepend'?'selected':''}>Внутрь (в начало)</option>
                <option value="before" ${el.placement==='before'?'selected':''}>Перед элементом</option>
                <option value="after" ${el.placement==='after'?'selected':''}>После элемента</option>
                <option value="replace" ${el.placement==='replace'?'selected':''}>Заменить элемент</option>
              </select>
              <button class="btn btn-danger btn-sm" onclick="removePageElement(${i})" style="flex-shrink:0"><i class="fas fa-trash"></i></button>
            </div>
            <div style="margin-bottom:6px">
              <label style="font-size:11px;color:var(--text3)">HTML-код элемента</label>
              <textarea placeholder='<button class="my-btn" onclick="alert(1)">Моя кнопка</button>' oninput="updatePageElement(${i},'html',this.value)" style="width:100%;min-height:60px;font-family:monospace;font-size:12px">${escHtml(el.html||'')}</textarea>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <div style="flex:1;min-width:200px">
                <label style="font-size:11px;color:var(--text3)">CSS-стили (опционально)</label>
                <textarea placeholder=".my-btn { background: #6366f1; color: #fff; padding: 8px 16px; border-radius: 8px; border: none; cursor: pointer; }" oninput="updatePageElement(${i},'css',this.value)" style="width:100%;min-height:50px;font-family:monospace;font-size:12px">${escHtml(el.css||'')}</textarea>
              </div>
              <div style="flex:1;min-width:200px">
                <label style="font-size:11px;color:var(--text3)">JavaScript (опционально)</label>
                <textarea placeholder="// Код выполнится после вставки элемента\ndocument.querySelector('.my-btn').addEventListener('click', function(){ alert('Нажали!'); });" oninput="updatePageElement(${i},'js',this.value)" style="width:100%;min-height:50px;font-family:monospace;font-size:12px">${escHtml(el.js||'')}</textarea>
              </div>
            </div>
            <div style="margin-top:6px">
              <label style="font-size:11px;color:var(--text3)">Python-код (опционально, серверная логика)</label>
              <textarea placeholder="print('hello from extension')" oninput="updatePageElement(${i},'python',this.value)" style="width:100%;min-height:40px;font-family:monospace;font-size:12px">${escHtml(el.python||'')}</textarea>
              <div style="display:flex;gap:14px;margin-top:6px;flex-wrap:wrap">
                <label style="font-size:11px;color:var(--text2);cursor:pointer;display:flex;align-items:center;gap:4px">
                  <input type="checkbox" ${el.run_on_load?'checked':''} onchange="updatePageElement(${i},'run_on_load',this.checked)">
                  <span>Выполнять при заходе в модуль</span>
                </label>
                <label style="font-size:11px;color:var(--text2);cursor:pointer;display:flex;align-items:center;gap:4px">
                  <input type="checkbox" ${el.call_python?'checked':''} onchange="updatePageElement(${i},'call_python',this.checked)">
                  <span>Вызывать Python из браузера при загрузке элемента</span>
                </label>
              </div>
            </div>
          </div>
          `).join('')}
        </div>
        <button class="btn btn-secondary" onclick="addPageElement()" style="margin-top:6px"><i class="fas fa-plus"></i> Добавить элемент</button>
      </div>
      <div class="card" style="margin-top:16px;background:var(--bg3);border:1px dashed var(--border)">
        <div class="card-title"><i class="fas fa-robot" style="color:var(--accent)"></i> AI-генератор элемента</div>
        <p style="font-size:12px;color:var(--text3);margin:0 0 8px">Опишите, что нужно добавить на страницу — AI заполнит поля <b>HTML, CSS, JS</b> и <b>Python</b> нового элемента.</p>
        <div style="display:flex;gap:8px">
          <textarea id="aiElementPrompt" placeholder="Например: панель погоды с кнопкой обновления, виджет со списком последних заметок, кнопка «Сохранить в избранное» с сохранением на сервере..." style="flex:1;min-height:44px;padding:8px;border-radius:6px;border:1px solid var(--bg4);background:var(--bg2);color:var(--text1);font-size:13px;resize:vertical"></textarea>
          <button class="btn btn-primary" onclick="aiAddElement()" id="aiElementBtn" style="height:44px;white-space:nowrap"><i class="fas fa-wand-magic-sparkles"></i> Добавить</button>
        </div>
        <div id="aiElementStatus" style="margin-top:6px;font-size:12px;color:var(--text2);display:none"></div>
      </div>
    </div>
    ${navButtons(6)}
  </div>`;
}

/* ====== Step 7: Module Pages ====== */
function renderModulePages(){
  const pages = state.module_pages.pages || [];
  const icons = data.icons || [];
  return `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-globe" style="color:var(--accent)"></i> Модульные страницы</h2>
      <p><b>Страницы модуля</b> — это отдельные веб-страницы вашего расширения. Например: страница статистики, панель управления, страница с формой обратной связи.</p>
      <p style="font-size:12px;color:var(--text3);margin-top:4px">Каждая страница будет доступна по адресу: <code>/module/${state.name||"ext"}/&lt;путь-страницы&gt;</code>. Если включена опция «Показывать в навигации» — ссылка появится в боковом меню Infinity AI.</p>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-pen"></i> Страницы расширения</div>
      <div id="modulePagesList">
        ${pages.length === 0 ? '<div style="padding:16px;text-align:center;color:var(--text3);font-style:italic">Нет страниц. Нажмите «Добавить страницу».</div>' : ''}
        ${pages.map((p,i) => `
        <div class="mp-item" data-idx="${i}">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <select class="mp-icon-select" onchange="updateModulePage(${i},'icon',this.value)" style="width:100px">
              ${icons.map(ic => `<option value="${ic}" ${p.icon===ic?'selected':''}>${ic}</option>`).join('')}
            </select>
            <input type="text" value="${escHtml(p.title||'')}" oninput="updateModulePage(${i},'title',this.value)" placeholder="Заголовок" style="flex:1;min-width:120px">
            <input type="text" value="${escHtml(p.path||'')}" oninput="updateModulePage(${i},'path',this.value)" placeholder="URL-путь" style="flex:1;min-width:100px">
            <select onchange="updateModulePage(${i},'template',this.value)" style="width:100px">
              <option value="full" ${p.template==='full'?'selected':''}>Полная</option>
              <option value="panel" ${p.template==='panel'?'selected':''}>Панель</option>
            </select>
            <button class="btn btn-danger btn-sm" onclick="removeModulePage(${i})" style="flex-shrink:0"><i class="fas fa-trash"></i></button>
          </div>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
            <textarea placeholder="CSS (опционально)" oninput="updateModulePage(${i},'css',this.value)" style="flex:1;min-height:50px;font-family:monospace;font-size:12px">${escHtml(p.css||'')}</textarea>
            <textarea placeholder="HTML (опционально)" oninput="updateModulePage(${i},'html',this.value)" style="flex:1;min-height:50px;font-family:monospace;font-size:12px">${escHtml(p.html||'')}</textarea>
          </div>
          <div style="margin-top:6px">
            <label style="font-size:12px;color:var(--text3)">
              <input type="checkbox" ${p.in_nav!==false?'checked':''} onchange="updateModulePage(${i},'in_nav',this.checked)">
              Показывать в навигации
            </label>
            <input type="text" value="${escHtml(p.nav_section||'')}" oninput="updateModulePage(${i},'nav_section',this.value)" placeholder="Раздел навигации (опц.)" style="margin-left:12px;width:200px">
          </div>
        </div>
        `).join('')}
      </div>
      <button class="btn btn-secondary" onclick="addModulePage()" style="margin-top:10px"><i class="fas fa-plus"></i> Добавить страницу</button>
    </div>
    <div class="card" style="background:var(--bg3);border:1px dashed var(--border)">
      <div class="card-title"><i class="fas fa-info-circle"></i> Как это работает</div>
      <ul style="font-size:13px;color:var(--text2);padding-left:20px">
        <li>После установки расширения страницы доступны по адресу: <code>/module/${state.name||"ext"}/&lt;путь&gt;</code> — просто откройте этот URL в браузере</li>
        <li>Вы можете создать несколько страниц — у каждой будет свой URL, свой HTML и свой CSS</li>
        <li>HTML и CSS применяются на сервере — они увидят все посетители страницы</li>
        <li>Внутри HTML доступен объект <code>ExtensionRuntime</code> — можно показывать уведомления, менять DOM, делать API-запросы</li>
        <li>Опция «Показывать в навигации» — если включена, ссылка на страницу появится в боковом меню Infinity AI</li>
        <li><b>Как редактировать?</b> После создания расширения откройте его в списке расширений → нажмите «Редактировать» → выберите файл <code>extension.json</code> и измените секцию <code>module_pages</code></li>
      </ul>
    </div>
    ${navButtons(7)}
  </div>`;
}

/* ====== Page Element helpers ====== */
function addPageElement(){
  if(!state.page_elements) state.page_elements = [];
  if(!state.create_frontend){
    state.create_frontend = true;
    showToast("Автоматически включён frontend.js для поддержки элементов", "info");
  }
  state.page_elements.push({selector:"", placement:"append", html:"", css:"", js:"", python:"", run_on_load:false, call_python:false});
  renderStep(6);
}
function updatePageElement(idx, field, value){
  if(!state.page_elements) state.page_elements = [];
  if(!state.page_elements[idx]) return;
  state.page_elements[idx][field] = value;
}
function removePageElement(idx){
  if(!state.page_elements) state.page_elements = [];
  if(idx >= 0 && idx < state.page_elements.length) state.page_elements.splice(idx, 1);
  renderStep(6);
}

function addModulePage(){
  const pages = state.module_pages.pages || [];
  pages.push({path:"page-"+(pages.length+1), title:"Новая страница", icon:"fa-globe", template:"full", css:"", html:"", in_nav:true, nav_section:""});
  state.module_pages.pages = pages;
  renderStep(7);
}

function updateModulePage(idx, field, value){
  const pages = state.module_pages.pages || [];
  if(!pages[idx]) return;
  pages[idx][field] = value;
}

function removeModulePage(idx){
  const pages = state.module_pages.pages || [];
  if(idx >= 0 && idx < pages.length) pages.splice(idx, 1);
  state.module_pages.pages = pages;
  renderStep(7);
}

/* ====== Step 8: Generate ====== */
function renderGenerate(){
  const hasName = state.name.trim().length > 0;
  const hasModule = state.target_modules.length > 0 || state.target_module === "__new__";
  const isEdit = !!state.editMode;
  return `
  <div class="step-panel active">
    <div class="step-header">
      <h2><i class="fas fa-rocket" style="color:var(--accent)"></i> ${isEdit ? 'Сохранение изменений' : 'Генерация расширения'}</h2>
      <p>${isEdit ? 'Проверьте конфигурацию и сохраните изменения в существующее расширение <b>${escHtml(state.editMode)}</b>.' : 'Проверьте конфигурацию и создайте ваше расширение.'}</p>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-clipboard-list"></i> Сводка</div>
      <div class="summary-grid">
        <div class="summary-item">
          <div class="s-label">Название</div>
          <div class="s-value">${escHtml(state.name||'—')} <span class="badge badge-module">v${state.version||'1.0.0'}</span></div>
        </div>
        <div class="summary-item">
          <div class="s-label">Автор</div>
          <div class="s-value">${escHtml(state.author||'—')}</div>
        </div>
        <div class="summary-item" style="grid-column:1/-1">
          <div class="s-label">Описание</div>
          <div class="s-value">${escHtml(state.description||'—')}</div>
        </div>
        <div class="summary-item">
          <div class="s-label">Модули</div>
          <div class="s-value">${state.target_modules.length > 0 ? state.target_modules.map(m => `<span class="badge badge-module tag"><i class="fas ${getModIcon(m)}"></i> ${data.module_info?.[m]||m}</span>`).join(" "): state.target_module==='__new__' ? `<span class="badge badge-module tag"><i class="fas fa-plus"></i> ${state.new_module?.display_name||state.new_module?.name||'Новый'}</span>` : '—'}</div>
        </div>
        <div class="summary-item">
          <div class="s-label">Хуки (${state.hooks.length})</div>
          <div class="s-value">${state.hooks.length > 0 ? state.hooks.map(h => {
            const acts = state.hook_actions[h] || [];
            const badges = acts.map(a => `<span class="action-badge ${a.type}" style="font-size:9px;margin:1px"><i class="fas ${ACTION_TYPES[a.type]?.icon||'fa-code'}"></i> ${ACTION_TYPES[a.type]?.label||a.type}</span>`).join('');
            return `<span style="display:inline-flex;align-items:center;gap:4px;margin:2px 0"><span class="badge badge-hook tag">${h}</span>${badges ? '→ ' + badges : ''}</span>`;
          }).join(" ") : '—'}</div>
        </div>
        <div class="summary-item">
          <div class="s-label">Разрешения (${state.permissions.length})</div>
          <div class="s-value">${state.permissions.map(p => `<span class="badge badge-module tag">${p}</span>`).join(" ")}</div>
        </div>
        <div class="summary-item">
          <div class="s-label">Конфиг (${state.config_schema.length} полей)</div>
          <div class="s-value">${state.config_schema.length > 0 ? state.config_schema.map(f => `<span class="badge badge-module tag">${f.id||f.label||'?'}</span>`).join(" ") : '—'}</div>
        </div>
        <div class="summary-item">
          <div class="s-label">Frontend</div>
          <div class="s-value">${state.create_frontend ? '<span class="badge badge-hook">включён</span>' : '<span style="color:var(--text3)">не требуется</span>'}</div>
        </div>
        ${(state.module_pages.pages||[]).length > 0 ? `
        <div class="summary-item">
          <div class="s-label">Страницы (${(state.module_pages.pages||[]).length})</div>
          <div class="s-value">${(state.module_pages.pages||[]).map(p => `<span class="badge badge-module tag"><i class="fas ${p.icon||'fa-globe'}"></i> ${escHtml(p.title)}</span>`).join(' ')}</div>
        </div>` : ''}
      </div>
    </div>
    <div class="card">
      <div class="card-title"><i class="fas fa-code"></i> Предпросмотр кода</div>
      <div class="code-preview" id="codePreview">
        <div class="code-header" id="codeTabs">
          <span class="file-tab active" data-file="extension.json" onclick="switchPreviewFile('extension.json')">extension.json</span>
          <span class="file-tab" data-file="main.py" onclick="switchPreviewFile('main.py')">main.py</span>
          ${state.create_frontend ? '<span class="file-tab" data-file="frontend.js" onclick="switchPreviewFile(\'frontend.js\')">frontend.js</span>' : ''}
          <div style="flex:1"></div>
          <button class="btn btn-sm" onclick="startAiImprove()" id="aiImproveBtn" style="padding:2px 10px;font-size:11px"><i class="fas fa-wand-magic-sparkles"></i> AI улучшить</button>
        </div>
        <div id="monacoContainer" style="height:420px;border-radius:0 0 8px 8px"></div>
      </div>
    </div>
    <div class="card" id="aiImprovePanel" style="display:none">
      <div class="card-title"><i class="fas fa-wand-magic-sparkles" style="color:var(--accent)"></i> AI-улучшение кода</div>
      <div style="display:flex;gap:8px;margin-top:8px">
        <textarea id="aiImprovePrompt" placeholder="Что улучшить? (Добавить обработку ошибок, переписать на async, добавить логирование...)" style="flex:1;min-height:50px;padding:8px;border-radius:6px;border:1px solid var(--bg4);background:var(--bg2);color:var(--text1);font-size:13px;resize:vertical"></textarea>
        <button class="btn btn-primary" onclick="doAiImprove()" id="aiImproveDoBtn" style="height:50px;white-space:nowrap"><i class="fas fa-wand-magic-sparkles"></i> Улучшить</button>
      </div>
      <div id="aiImproveStatus" style="margin-top:6px;font-size:12px;color:var(--text2)"></div>
    </div>
    <div class="card" id="testPanel" style="display:none">
      <div class="card-title"><i class="fas fa-flask" style="color:var(--green)"></i> Результаты тестирования</div>
      <div id="testResults" style="font-size:13px"></div>
    </div>
    <div class="card" style="background:var(--bg3);border:1px dashed var(--border)">
      <div class="card-title"><i class="fas fa-question-circle"></i> Часто задаваемые вопросы</div>
      <div style="font-size:12px;color:var(--text2);line-height:1.6">
        <details><summary><b>Как зайти в мой новый модуль после создания?</b></summary>
        После нажатия «Создать расширение» оно появится в списке расширений (страница <b>/apps#extensions</b>). Если вы создали модуль — его страница будет доступна по пути <code>/module/&lt;имя-расширения&gt;</code>. Также в боковом меню Infinity AI появится ссылка на модуль.
        </details>
        <details><summary><b>Как редактировать расширение после создания?</b></summary>
        Откройте страницу <b>/apps#extensions</b>, найдите ваше расширение в списке и нажмите <b>«Редактировать»</b>. Откроется редактор кода, где можно менять <code>extension.json</code>, <code>main.py</code>, <code>frontend.js</code> и другие файлы.
        </details>
        <details><summary><b>Что такое frontend.js и зачем он нужен?</b></summary>
        <b>Frontend.js</b> — это JavaScript-код, который выполняется в браузере пользователя. Он нужен, чтобы добавлять кнопки, панели, менять внешний вид страницы. Без него расширение работает только на сервере (хуки, API, Python). Включите «Создать frontend.js» на шаге 6, если хотите визуально изменять интерфейс.
        </details>
        <details><summary><b>Как опубликовать расширение для других пользователей?</b></summary>
        На странице <b>/apps#extensions</b> нажмите на карточку вашего расширения, затем кнопку <b>«Опубликовать»</b>. Укажите название, описание и категорию. После публикации расширение появится в магазине расширений, и другие пользователи смогут его установить.
        </details>
        <details><summary><b>Чем отличаются хуки (шаг 2) от элементов страницы (шаг 6)?</b></summary>
        <b>Хуки</b> — это серверные события (открытие книги, поиск, загрузка). Они выполняют Python-код на сервере, невидимый пользователю. <b>Элементы страницы</b> — это HTML/CSS/JS, которые вставляются прямо в браузер. Хуки хороши для логики (сохранить данные, сделать запрос), элементы — для интерфейса (кнопки, панели, стили). Можно использовать и то, и другое вместе.
        </details>
        <details><summary><b>Можно ли создать расширение без единой строчки кода?</b></summary>
        <b>Да!</b> Используйте AI-ассистента на шаге 1: опишите словами, что должно делать расширение, и AI сгенерирует всю конфигурацию. Также можно использовать готовые шаблоны (Минимальное, Поисковый, UI Виджет, etc.) — выберите подходящий и донастройте в визуальном редакторе.
        </details>
      </div>
    </div>
    <div class="nav-buttons" style="justify-content:center;gap:14px">
      <button class="btn btn-secondary btn-lg" onclick="showStep(7)"><i class="fas fa-arrow-left"></i> Назад</button>
      <button class="btn btn-info btn-lg" onclick="livePreview()" id="pvBtn">
        <i class="fas fa-eye"></i> Предпросмотр
      </button>
      <button class="btn btn-warning btn-lg" onclick="testExtension()" id="testBtn">
        <i class="fas fa-flask"></i> Тест
      </button>
      <button class="btn btn-primary btn-lg" onclick="generateAndInstall()" id="genBtn" ${!hasName?'disabled':''}>
        ${isEdit ? '<i class="fas fa-save"></i> Сохранить изменения' : '<i class="fas fa-rocket"></i> Создать расширение'}
      </button>
      <button class="btn btn-success btn-lg" onclick="generateAndDownload()" id="dlBtn" ${!hasName?'disabled':''}>
        <i class="fas fa-download"></i> Скачать .zip
      </button>
      <button class="btn btn-info btn-lg" onclick="generateAndDownloadInf()" id="infBtn" ${!hasName?'disabled':''}
              title="Один файл .inf: двойной клик в файловом менеджере запускает его как приложение">
        <i class="fas fa-box"></i> Скачать .inf
      </button>
    </div>
    ${!hasName ? '<div style="text-align:center;margin-top:10px;color:var(--red);font-size:13px"><i class="fas fa-exclamation-circle"></i> Укажите название расширения на шаге 1</div>' : ''}
  </div>`;
  // Note: we also need to check module
}

function buildExtensionJson(){
  const module = state.target_module === "__new__" ? (state.new_module?.name||"custom") :
    (state.target_modules.includes("all") ? "all" : (state.target_modules[0]||"all"));
  const meta = {
    name: state.name,
    version: state.version,
    description: state.description,
    author: state.author,
    target_module: module,
    hooks: state.hooks,
    entry_point: "main.py",
    frontend_entry: state.create_frontend ? "frontend.js" : "",
    permissions: state.permissions,
    icon: state.icon,
    dependencies: state.dependencies,
    config: {},
    config_schema: state.config_schema.reduce((acc,f)=>{
      if(f.id) acc[f.id] = {type:f.type,default:f.default,description:f.description,label:f.label||f.id};
      return acc;
    },{}),
    sandbox: state.sandbox,
    auto_update: state.auto_update,
    update_url: state.update_url,
    module_pages: state.module_pages || {},
    source: "builder"
  };
  return meta;
}

function buildMainPy(){
  const module = getTargetModule();
  let code = `"""\n${state.name} — Extension for ${module}\nAuto-generated by Visual Extension Builder\n"""\n\n`;
  code += `import logging\nimport asyncio\nimport json\nfrom extension_runtime import log_message, show_notification, api_request, modify_dom, open_url\n\n`;
  code += `logger = logging.getLogger("infinity")\n\n\n`;

  // Generate hook functions with actions
  state.hooks.forEach(hook => {
    const hookSlug = hook.replace("on_","",1);
    const actions = state.hook_actions[hook] || [];
    code += `def ${hook}(**kwargs):\n`;
    code += `    results = []\n`;
    if(actions.length === 0){
      code += `    r = log_message("${state.name}: ${hook} called")\n    results.append(r)\n    return results\n\n`;
      return;
    }
    actions.forEach((action, ai) => {
      const act = action.type;
      const p = action.params || {};
      const cond = (action.condition || "").trim();
      code += `    # Action ${ai+1}: ${ACT_LABEL[act]||act}\n`;
      const baseIndent = cond ? "        " : "    ";
      let body = "";

      if(act === "log"){
        body += `${baseIndent}r = log_message(message="${escPy(p.message||`${hook} called`)}", level="${p.level||"info"}")\n`;
      } else if(act === "notify"){
        body += `${baseIndent}r = show_notification(title="${escPy(p.title||"")}", message="${escPy(p.message||"")}", type="${p.type||"info"}")\n`;
      } else if(act === "api_call"){
        body += `${baseIndent}try:\n`;
        body += `${baseIndent}    import asyncio, concurrent.futures\n`;
        body += `${baseIndent}    with concurrent.futures.ThreadPoolExecutor() as _pool:\n`;
        body += `${baseIndent}        _fut = _pool.submit(asyncio.run, api_request(method="${p.method||"GET"}", url="${escPy(p.url||"")}", headers=${p.headers||"{}"}, data=${p.body ? `"""${escPy(p.body)}"""` : "None"}))\n`;
        body += `${baseIndent}        r = _fut.result(timeout=30)\n`;
        body += `${baseIndent}except Exception as e:\n`;
        body += `${baseIndent}    r = {"error": str(e)}\n`;
      } else if(act === "modify_dom"){
        body += `${baseIndent}r = modify_dom(selector="${escPy(p.selector||"")}", action="${p.action||"show"}", value="${escPy(p.value||"")}")\n`;
      } else if(act === "redirect"){
        body += `${baseIndent}r = open_url(url="${escPy(p.url||"")}", target="${p.target||"self"}")\n`;
      } else if(act === "custom"){
        const lines = (p.code||"pass").split("\n");
        body += `${baseIndent}try:\n`;
        lines.forEach(l => { body += `${baseIndent}    ${l.replace(/\s+$/,"")}\n`; });
        body += `${baseIndent}    r = {"action": "custom", "output": "executed"}\n`;
        body += `${baseIndent}except Exception as _e:\n`;
        body += `${baseIndent}    r = {"error": str(_e)}\n`;
      } else {
        body += `${baseIndent}r = log_message("${escPy(state.name)}: ${escPy(hook)} action ${escPy(act)}")\n`;
      }
      body += `${baseIndent}results.append(r)\n`;

      if(cond){
        code += `    if ${cond}:\n`;
        code += body.split("\n").map(l => l ? "    " + l : "").join("\n") + "\n";
      } else {
        code += body;
      }
    });
    // Default return
    code += `    return results\n\n`;
  });

  // Fallback for hooks with no actions
  state.hooks.forEach(hook => {
    if(hook !== "on_load" && !state.hook_actions[hook]){
      const slug = hook.replace("on_","",1);
      code += `# ${hook} (no actions configured)\n`;
    }
  });

  // Python handlers for page elements
  var hasRunOnLoad = false;
  if(state.page_elements && state.page_elements.length > 0){
    state.page_elements.forEach(function(el, ei){
      if(el.python && el.python.trim()){
        var pyCode = el.python;
        code += `\n# Python handler for page element ${ei}: ${el.selector}\n`;
        code += `async def element_${ei}_handler(**kwargs):\n`;
        code += `    try:\n`;
        var pyLines = pyCode.split("\n");
        pyLines.forEach(function(l){ code += `    ${l}\n`; });
        code += `        return {"ok": True, "result": "executed"}\n`;
        code += `    except Exception as e:\n`;
        code += `        return {"ok": False, "error": str(e)}\n\n`;
        if(el.run_on_load){
          hasRunOnLoad = true;
        }
      }
    });
  }

  // Auto-generated on_load hook for page elements with run_on_load
  if(hasRunOnLoad){
    code += `async def on_load(**kwargs):\n`;
    code += `    results = []\n`;
    state.page_elements.forEach(function(el, ei){
      if(el.python && el.python.trim() && el.run_on_load){
        code += `    r = await element_${ei}_handler(**kwargs)\n`;
        code += `    results.append(r)\n`;
      }
    });
    code += `    return results\n\n`;
  }

  const allApis = [
    ...state.pinned_apis.map(p => p),
    ...state.custom_apis.map(a => `${a.method} ${a.path}`)
  ];
  if(allApis.length > 0){
    code += `\n# API endpoints used by this extension:\n`;
    allApis.forEach(p => { code += `# - ${p}\n`; });
  }
  return code;
}

function escPy(s){
  return String(s).replace(/\\/g,"\\\\").replace(/"/g,'\\"').replace(/\n/g,"\\n").replace(/\r/g,"");
}

function escJs(s){
  return String(s).replace(/\\/g,"\\\\").replace(/`/g,"\\`").replace(/\${/g,"\\${").replace(/'/g,"\\'").replace(/\n/g,"\\n").replace(/\r/g,"");
}

function escJsQ(s){
  return String(s).replace(/\\/g,"\\\\").replace(/'/g,"\\'").replace(/\n/g,"\\n").replace(/\r/g,"");
}

const ACT_LABEL = {};
Object.entries(ACTION_TYPES).forEach(([k,v]) => { ACT_LABEL[k] = v.label; });

function buildFrontendJs(){
  if(!state.create_frontend) return "";
  const module = getTargetModule();
  const btnText = state.frontend_config.button_text || state.name;
  const btnIcon = (state.frontend_config.button_icon || state.icon).replace("fa-","");
  let code = `// ${state.name} — frontend.js\n// Auto-generated by Visual Extension Builder\n\n(function(){\n`;
  code += `    document.body.setAttribute('data-extension-name','${state.name}');\n`;
  code += `    var RT = window.ExtensionRuntime;\n`;
  code += `    if(!RT){ console.warn('[${state.name}] ExtensionRuntime not found'); return; }\n`;
  code += `    RT.log('[${state.name}] Loaded for module: ${module}');\n`;
  if(state.frontend_config.add_styles){
    code += `\n    RT.addStyles(\`\n`;
    code += `        .ext-${state.name}-badge {\n`;
    code += `            display: inline-flex; align-items: center; gap: 6px;\n`;
    code += `            padding: 4px 10px;\n`;
    code += `            background: linear-gradient(135deg, #6366f1, #a855f7);\n`;
    code += `            color: #fff; border-radius: 20px;\n`;
    code += `            font-size: 11px; font-weight: 600; cursor: pointer;\n`;
    code += `        }\n`;
    code += `        .ext-${state.name}-panel {\n`;
    code += `            background: var(--bg2);\n`;
    code += `            border: 1px solid var(--border);\n`;
    code += `            border-radius: 10px;\n`;
    code += `            padding: 14px;\n`;
    code += `            margin: 10px 0;\n`;
    code += `        }\n`;
    code += `    \`);\n`;
  }
  if(state.frontend_config.inject_html){
    code += `\n    RT.injectHTML(\`${escJs(state.frontend_config.inject_html)}\`);\n`;
  }

  // Page elements insertion
  if(state.page_elements && state.page_elements.length > 0){
    state.page_elements.forEach(function(el, ei){
      if(!el.selector && !el.html && !el.css && !el.js && !el.python) return;
      if(el.css){
        code += `\n    RT.addStyles(\`${escJs(el.css)}\`);\n`;
      }
      if(el.selector && el.html){
        code += `\n    RT.modifyDom('${escJsQ(el.selector)}', '${escJsQ(el.placement)}', \`${escJs(el.html)}\`);\n`;
      } else if(el.html && !el.selector){
        code += `\n    RT.injectHTML(\`${escJs(el.html)}\`);\n`;
      }
      if(el.js){
        code += `\n    (function(){ ${el.js} })();\n`;
      }
      if(el.python){
        if(el.call_python){
          code += `\n    fetch('/api/extensions/${state.name}/page-element/${ei}', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({selector:'${escJsQ(el.selector||'')}'})}).then(function(r){ return r.json(); }).then(function(d){ if(d.ok) RT.log('[${state.name}] Python element ${ei} result: ' + JSON.stringify(d.result)); else RT.log('[${state.name}] Python element ${ei} error: ' + d.error); }).catch(function(e){ RT.log('[${state.name}] Python element ${ei} fetch error: ' + e.message); });\n`;
        } else {
          code += `\n    RT.log('[${state.name}] Python handler ready for element ${ei}');\n`;
        }
      }
    });
  }

  if(state.frontend_config.show_badge !== false){
    code += `\n    RT.addBadge('${escJsQ(state.name)}', '${escJsQ(btnIcon)}', '${escJsQ(btnText)}'`;
    if(state.frontend_config.show_notification){
      code += `, function(){ RT.showNotification('Extension "${escJsQ(state.name)} is active!"', 'info'); }`;
    }
    code += `);\n`;
  }

  code += `var _mp = ${JSON.stringify(state.module_pages.pages || [])};\n`;
  code += `if(_mp.length > 0){\n`;
  code += `    RT.registerModulePages('${state.name}', ${JSON.stringify(state.module_pages.pages || [])});\n`;
  code += `}\n`;

  code += `})();\n`;
  return code;
}

function getTargetModule(){
  if(state.target_module === "__new__") return state.new_module?.name||"custom";
  if(state.target_modules.includes("all")) return "all";
  return state.target_modules[0]||"all";
}

let previewFile = "extension.json";

function switchPreviewFile(file){
  previewFile = file;
  document.querySelectorAll("#codeTabs .file-tab").forEach(t => t.classList.toggle("active",t.dataset.file===file));
  let content = "";
  if(file === "extension.json") content = JSON.stringify(buildExtensionJson(),null,2);
  else if(file === "main.py") content = buildMainPy();
  else if(file === "frontend.js") content = buildFrontendJs();
  if(monacoEditor){
    monacoEditor.setValue(content);
    const lang = file === "extension.json" ? "json" : file === "main.py" ? "python" : "javascript";
    monaco.editor.setModelLanguage(monacoEditor.getModel(), lang);
  }
}

async function generateAndInstall(){
  const btn = document.getElementById("genBtn");
  const isEdit = !!state.editMode;
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> ' + (isEdit ? 'Сохранение...' : 'Создание...');
  try{
    let config = buildConfig();
    if(isEdit){
      config.update = true;
      if(state.editMode && state.editMode !== config.name){
        config.old_name = state.editMode;
        const check = await fetch("/api/extensions/" + encodeURIComponent(config.name) + "/check");
        const checkResult = await check.json();
        if(checkResult.exists){
          showToast("Расширение \"" + config.name + "\" уже существует. Выберите другое имя.", "error");
          btn.disabled = false;
          btn.innerHTML = '<i class="fas fa-save"></i> Сохранить изменения';
          return;
        }
      }
    } else {
      // Check if name exists and auto-suffix if needed
      const check = await fetch("/api/extensions/" + encodeURIComponent(config.name) + "/check");
      const checkResult = await check.json();
      if(checkResult.exists){
        let i = 1;
        let newName = config.name + "-" + i;
        while(true){
          const c2 = await fetch("/api/extensions/" + encodeURIComponent(newName) + "/check");
          const r2 = await c2.json();
          if(!r2.exists) break;
          i++;
          newName = config.name + "-" + i;
        }
        showToast("Расширение \"" + config.name + "\" уже существует. Создаём \"" + newName + "\"", "warning");
        config.name = newName;
      }
    }
    const r = await fetch("/api/extensions/generate-visual", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
    const result = await r.json();
    if(result.ok){
      showToast(isEdit ? "Изменения сохранены!" : "Расширение успешно создано!", "success");
      setTimeout(()=>{window.location.href="/apps#extensions";},1500);
    } else {
      let errMsg = result.error || "неизвестная";
      if(result.frontend_error && errMsg.indexOf(result.frontend_error) === -1){
        errMsg += " — " + result.frontend_error;
      }
      showToast("Ошибка: " + errMsg, "error");
      if(result.frontend_error) showJsErrorBanner(errMsg);
      btn.disabled = false;
      btn.innerHTML = isEdit ? '<i class="fas fa-save"></i> Сохранить изменения' : '<i class="fas fa-rocket"></i> Создать расширение';
    }
  }catch(e){
    showToast("Ошибка соединения: "+e.message, "error");
    btn.disabled = false;
    btn.innerHTML = isEdit ? '<i class="fas fa-save"></i> Сохранить изменения' : '<i class="fas fa-rocket"></i> Создать расширение';
  }
}

async function generateAndDownload(){
  const btn = document.getElementById("dlBtn");
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> Подготовка...';
  try{
    const config = buildConfig();
    // First generate on server, then export
    const r1 = await fetch("/api/extensions/generate-visual", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
    const result = await r1.json();
    if(result.ok){
      const r2 = await fetch(`/api/extensions/${config.name}/export`);
      if(r2.ok){
        const blob = await r2.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${config.name}.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast("Расширение скачано!", "success");
      }
    } else {
      showToast("Ошибка: "+ (result.error||"неизвестная"), "error");
    }
  }catch(e){
    showToast("Ошибка: "+e.message, "error");
  }
  btn.disabled = false;
  btn.innerHTML = '<i class="fas fa-download"></i> Скачать .zip';
}

// .inf — тот же пакет, но одним JSON-файлом. В файловом менеджере по нему
// открывается мини-окно установщика, а двойной клик по .inf типа app запускает
// его как приложение.
async function generateAndDownloadInf(){
  const btn = document.getElementById("infBtn");
  if(!btn) return;
  btn.disabled = true;
  const restore = () => { btn.disabled = false; btn.innerHTML = '<i class="fas fa-box"></i> Скачать .inf'; };
  btn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> Подготовка...';
  try{
    const config = buildConfig();
    const r1 = await fetch("/api/extensions/generate-visual", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
    const result = await r1.json();
    if(!result.ok){
      showToast("Ошибка: "+(result.error||"неизвестная"), "error");
      restore();
      return;
    }
    const r2 = await fetch("/api/extensions/" + encodeURIComponent(config.name) + "/export-inf");
    if(!r2.ok){
      const d = await r2.json().catch(()=>({}));
      showToast("Ошибка: " + (d.error || "не удалось собрать .inf"), "error");
      restore();
      return;
    }
    const blob = await r2.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${config.name}.inf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast("Файл .inf скачан!", "success");
  }catch(e){
    showToast("Ошибка: " + e.message, "error");
  }
  restore();
}

function buildConfig(){
  var hooks = state.hooks.slice();
  // Auto-add on_load hook if any page element has run_on_load
  if(state.page_elements && state.page_elements.some(function(el){ return el.run_on_load; })){
    if(!hooks.includes("on_load")) hooks.push("on_load");
  }
  const config = {
    name: state.name,
    version: state.version,
    description: state.description,
    author: state.author,
    icon: state.icon,
    target_module: getTargetModule(),
    target_modules: state.target_modules,
    hooks: hooks,
    hook_actions: state.hook_actions,
    permissions: state.permissions,
    config_schema: state.config_schema,
    create_frontend: state.create_frontend,
    frontend_config: state.frontend_config,
    page_elements: state.page_elements,
    module_pages: state.module_pages,
    pinned_apis: state.pinned_apis,
    custom_apis: state.custom_apis,
    dependencies: state.dependencies,
    sandbox: state.sandbox,
    auto_update: state.auto_update,
    update_url: state.update_url
  };
  if(state.target_module === "__new__" && state.new_module){
    config.new_module = state.new_module;
    config.target_module = "__new__";
  }
  return config;
}

function showToast(msg, type){
  const existing = document.querySelector(".toast");
  if(existing) existing.remove();
  const t = document.createElement("div");
  t.className = "toast show " + (type||"");
  t.innerHTML = `<i class="fas ${type==='success'?'fa-check-circle':type==='error'?'fa-exclamation-circle':'fa-info-circle'}"></i> ${msg}`;
  document.body.appendChild(t);
  setTimeout(()=>{t.classList.remove("show");setTimeout(()=>t.remove(),300);},3500);
}

function navButtons(step){
  return `
  <div class="nav-buttons">
    <div>
      ${step > 0 ? `<button class="btn btn-secondary" onclick="showStep(${step-1})"><i class="fas fa-arrow-left"></i> Назад</button>` : '<div></div>'}
    </div>
    <button class="btn btn-primary" onclick="showStep(${step+1})">Далее <i class="fas fa-arrow-right"></i></button>
  </div>`;
}

function escHtml(s){
  if(!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

/* ====== Monaco Editor ====== */
let monacoEditor = null;
let monacoReady = false;

function initMonacoEditor(){
  const container = document.getElementById("monacoContainer");
  if(!container) return;
  if(monacoEditor){
    monacoEditor.layout();
    return;
  }
  if(typeof require === "undefined"){
    container.innerHTML = '<div style="padding:20px;color:var(--text3);text-align:center"><i class="fas fa-exclamation-triangle"></i> Monaco editor not loaded</div>';
    return;
  }
  require.config({ paths: { vs: "/static/monaco/min/vs" } });
  require(["vs/editor/editor.main"], function(){
    const content = JSON.stringify(buildExtensionJson(),null,2);
    monacoEditor = monaco.editor.create(container, {
      value: content,
      language: "json",
      theme: "vs-dark",
      fontSize: 13,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      automaticLayout: true,
      tabSize: 2,
      wordWrap: "on",
      lineNumbers: "on",
      renderWhitespace: "selection"
    });
    monacoReady = true;
  });
}

/* ====== AI Generate (SSE with live progress) ====== */
async function readSSE(resp, handlers){
  if(!resp.ok || !resp.body){
    let msg = "Ошибка сервера: " + resp.status;
    try{
      const j = await resp.json();
      if(j && j.error) msg = j.error;
    }catch(e){}
    throw new Error(msg);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while(true){
    const {done, value} = await reader.read();
    if(done) break;
    buffer += decoder.decode(value, {stream:true});
    let idx;
    while((idx = buffer.indexOf("\n\n")) !== -1){
      const raw = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 2);
      if(!raw.startsWith("data:")) continue;
      const payload = raw.slice(5).trim();
      if(payload === "[DONE]") continue;
      let evt;
      try{ evt = JSON.parse(payload); }catch(e){ continue; }
      handlers(evt);
    }
  }
}

const AI_STEP_LABELS = {
  analyze: "Анализ",
  module: "Модуль",
  python: "Python",
  interface: "Интерфейс",
  pages: "Страницы",
  finalize: "Сборка"
};

function aiLogLine(log, step, message, done){
  const row = document.createElement("div");
  row.className = "ai-log-line" + (done ? " done" : " active");
  row.innerHTML = '<span class="ic">' + (done ? '<i class="fas fa-check-circle"></i>' : '<i class="fas fa-spinner fa-pulse"></i>') + '</span>' +
    (step && AI_STEP_LABELS[step] ? '<span class="tag">' + AI_STEP_LABELS[step] + '</span>' : '') +
    '<span class="msg"></span>';
  row.querySelector(".msg").textContent = message || "";
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

async function aiGenerate(){
  const prompt = document.getElementById("aiPrompt");
  const btn = document.getElementById("aiGenBtn");
  const status = document.getElementById("aiStatus");
  const logWrap = document.getElementById("aiLogWrap");
  const log = document.getElementById("aiLog");
  const desc = prompt.value.trim();
  if(!desc){ showToast("Введите описание расширения", "error"); return; }
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> Работаю...';
  status.style.display = "block";
  status.innerHTML = '<span style="color:var(--text2)"><i class="fas fa-spinner fa-pulse"></i> AI генерирует расширение...</span>';
  logWrap.style.display = "block";
  log.innerHTML = "";
  let lastRow = null;
  let finalConfig = null;
  try{
    const r = await fetch("/api/extensions/builder/ai-build", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({description: desc, task:"full"})
    });
    await readSSE(r, function(evt){
      if(evt.type === "progress"){
        if(lastRow){ lastRow.classList.remove("active"); lastRow.classList.add("done"); lastRow.querySelector(".ic").innerHTML = '<i class="fas fa-check-circle"></i>'; }
        lastRow = aiLogLine(log, evt.step, evt.message, false);
        status.innerHTML = '<span style="color:var(--text2)"><i class="fas fa-spinner fa-pulse"></i> ' + evt.message + '</span>';
      } else if(evt.type === "result"){
        finalConfig = evt.config || null;
      } else if(evt.type === "warning"){
        aiLogLine(log, "warn", '<span style="color:#f0a500"><i class="fas fa-triangle-exclamation"></i> ' + evt.message + '</span>', true);
      } else if(evt.type === "error"){
        throw new Error(evt.error || "Ошибка генерации");
      }
    });
    if(!finalConfig) throw new Error("AI не вернул конфигурацию");
    if(lastRow){ lastRow.classList.remove("active"); lastRow.classList.add("done"); lastRow.querySelector(".ic").innerHTML = '<i class="fas fa-check-circle"></i>'; }
    aiLogLine(log, "finalize", "Готово! Расширение сгенерировано — код заполнен.", true);

    // Apply generated config to state
    const cfg = finalConfig;
    state.name = cfg.name || "ai-generated-ext";
    state.version = cfg.version || "1.0.0";
    state.description = cfg.description || desc;
    state.author = cfg.author || "Generated by AI";
    state.icon = (cfg.icon && cfg.icon.startsWith("fa-")) ? cfg.icon : "fa-puzzle-piece";
    if(cfg.new_module && cfg.new_module.name){
      state.new_module = {name: cfg.new_module.name, display_name: cfg.new_module.display_name || cfg.new_module.name, description: cfg.new_module.description || "", hooks: (cfg.new_module.hooks||[]).map(h => ({id: h.id, description: h.description||""}))};
      state.target_module = "__new__";
      state.target_modules = [cfg.new_module.name];
    } else {
      state.new_module = null;
      state.target_module = cfg.target_module || "all";
      state.target_modules = (cfg.target_modules && cfg.target_modules.length) ? cfg.target_modules : (cfg.target_module === "all" ? ["all"] : [cfg.target_module || "all"]);
    }
    state.hooks = cfg.hooks || [];
    state.hook_actions = cfg.hook_actions || {};
    state.permissions = cfg.permissions || ["storage"];
    state.config_schema = cfg.config_schema || [];
    state.create_frontend = !!cfg.create_frontend || (cfg.page_elements || []).length > 0;
    state.frontend_config = cfg.frontend_config || {};
    state.page_elements = cfg.page_elements || [];
    state.module_pages = cfg.module_pages || {};
    state.dependencies = cfg.dependencies || [];

    status.innerHTML = '<span style="color:var(--green)"><i class="fas fa-check-circle"></i> Расширение сгенерировано! Код заполнен — проверьте вкладку «Интерфейс» и «Действия».</span>';
    setTimeout(()=>{ status.style.display = "none"; }, 8000);
    showToast("Расширение сгенерировано AI с кодом!", "success");
    showStep(6);
  }catch(e){
    if(lastRow){ lastRow.classList.remove("active"); lastRow.querySelector(".ic").innerHTML = '<i class="fas fa-exclamation-circle"></i>'; lastRow.querySelector(".ic").style.color = "var(--red)"; }
    aiLogLine(log, "finalize", "Ошибка: " + e.message, true).querySelector(".ic").style.color = "var(--red)";
    status.innerHTML = '<span style="color:var(--red)"><i class="fas fa-exclamation-circle"></i> Ошибка: ' + e.message + '</span>';
  }
  btn.disabled = false;
  btn.innerHTML = '<i class="fas fa-wand-magic-sparkles"></i> Сгенерировать';
}

/* ====== AI: add page element on Interface tab ====== */
async function aiAddElement(){
  const prompt = document.getElementById("aiElementPrompt");
  const btn = document.getElementById("aiElementBtn");
  const status = document.getElementById("aiElementStatus");
  const desc = (prompt && prompt.value || "").trim();
  if(!desc){ showToast("Опишите элемент", "error"); return; }
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:13px;height:13px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> Генерация...';
  status.style.display = "block";
  status.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> AI пишет HTML/CSS/JS/Python...';
  try{
    const r = await fetch("/api/extensions/builder/ai-build", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({description: desc, task:"element", state: JSON.parse(JSON.stringify(state)), edit_mode: !!state.editMode, existing_elements: (state.page_elements||[])})
    });
    let element = null;
    let elementsList = null;
    let lastMsg = "";
    await readSSE(r, function(evt){
      if(evt.type === "progress"){
        lastMsg = evt.message;
        status.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> ' + evt.message;
      } else if(evt.type === "result"){
        if (evt.elements && evt.elements.length){ elementsList = evt.elements; }
        else { element = evt.element || null; }
      } else if(evt.type === "warning"){
        lastMsg = '<span style="color:#f0a500">' + evt.message + '</span>';
        status.innerHTML = '<i class="fas fa-triangle-exclamation"></i> ' + evt.message;
      } else if(evt.type === "error"){
        throw new Error(evt.error || "Ошибка генерации");
      }
    });
    if(elementsList){
      // Edit mode: AI вернул ПОЛНЫЙ обновлённый набор элементов — заменяем целиком
      state.page_elements = elementsList;
    } else {
      if(!element) throw new Error("AI не вернул элемент");
      if(!state.page_elements) state.page_elements = [];
      state.page_elements.push(element);
    }
    if(!state.create_frontend) state.create_frontend = true;
    status.innerHTML = '<span style="color:var(--green)"><i class="fas fa-check-circle"></i> Элемент добавлен! Поля HTML/CSS/JS/Python заполнены — можно отредактировать.</span>';
    setTimeout(()=>{ status.style.display = "none"; }, 6000);
    renderStep(6);
    showToast("Элемент сгенерирован AI!", "success");
  }catch(e){
    status.innerHTML = '<span style="color:var(--red)"><i class="fas fa-exclamation-circle"></i> Ошибка: ' + e.message + '</span>';
  }
  btn.disabled = false;
  btn.innerHTML = '<i class="fas fa-wand-magic-sparkles"></i> Добавить';
}

/* ====== AI Improve ====== */
function startAiImprove(){
  const panel = document.getElementById("aiImprovePanel");
  panel.style.display = panel.style.display === "none" ? "block" : "none";
}

async function doAiImprove(){
  const prompt = document.getElementById("aiImprovePrompt");
  const btn = document.getElementById("aiImproveDoBtn");
  const status = document.getElementById("aiImproveStatus");
  const instructions = prompt.value.trim() || "Improve this code, fix any issues, add better error handling and logging.";
  if(!monacoEditor) return;
  const code = monacoEditor.getValue();
  btn.disabled = true; btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> Улучшение...';
  status.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> AI анализирует код...';
  try{
    const r = await fetch("/api/extensions/builder/ai-improve", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code, instructions})
    });
    const result = await r.json();
    if(result.ok && result.code){
      monacoEditor.setValue(result.code);
      status.innerHTML = '<span style="color:var(--green)"><i class="fas fa-check-circle"></i> Код улучшен!</span>';
      showToast("Код улучшен AI!", "success");
    } else {
      status.innerHTML = '<span style="color:var(--red)"><i class="fas fa-exclamation-circle"></i> ' + (result.error||"Ошибка") + '</span>';
    }
  }catch(e){
    status.innerHTML = '<span style="color:var(--red)"><i class="fas fa-exclamation-circle"></i> Ошибка: ' + e.message + '</span>';
  }
  btn.disabled = false;
  btn.innerHTML = '<i class="fas fa-wand-magic-sparkles"></i> Улучшить';
}

/* ====== Live Preview ====== */
async function livePreview(){
  const btn = document.getElementById("pvBtn");
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> Загрузка...';
  try{
    const config = buildConfig();
    const r = await fetch("/api/extensions/builder/preview", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(config)
    });
    if(r.ok){
      const html = await r.text();
      const w = window.open("", "_blank", "width=900,height=700,scrollbars=yes");
      if(w){
        w.document.write(html);
        w.document.close();
      } else {
        // If popup blocked, show in modal
        const m = document.createElement("div");
        m.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:10000;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center";
        m.innerHTML = '<div style="background:#1a1a2e;border-radius:12px;padding:20px;max-width:700px;width:90%;max-height:80vh;overflow:auto"><div style="display:flex;justify-content:space-between;margin-bottom:10px"><h3 style="font-size:16px">Предпросмотр</h3><button onclick="this.parentElement.parentElement.parentElement.remove()" style="background:none;border:none;color:#fff;font-size:20px;cursor:pointer">&times;</button></div><iframe srcdoc="' + escHtml(html) + '" style="width:100%;height:500px;border:none;border-radius:8px"></iframe></div>';
        document.body.appendChild(m);
      }
    } else {
      showToast("Ошибка предпросмотра", "error");
    }
  }catch(e){
    showToast("Ошибка: "+e.message, "error");
  }
  btn.disabled = false;
  btn.innerHTML = '<i class="fas fa-eye"></i> Предпросмотр';
}

/* ====== Test Simulation ====== */
async function testExtension(){
  const btn = document.getElementById("testBtn");
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="width:14px;height:14px;border-color:rgba(255,255,255,0.3);border-top-color:#fff"></div> Тестирование...';
  const panel = document.getElementById("testPanel");
  const results = document.getElementById("testResults");
  panel.style.display = "none";

  try{
    const hook = state.hooks[0] || "on_load";
    const actions = state.hook_actions[hook] || [];
    const mockKwargs = {data: {query: "test", results: []}};

    const r = await fetch("/api/extensions/builder/test-simulate", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({hook, actions, kwargs: mockKwargs})
    });
    const res = await r.json();

    if(!res.ok){ showToast("Ошибка: "+(res.error||""), "error"); btn.disabled = false; btn.innerHTML = '<i class="fas fa-flask"></i> Тест'; return; }

    let html = `<div style="margin-bottom:10px">Тестируем хук: <strong>${res.hook}</strong> (${res.total} действий)</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr style="background:var(--bg3)">
        <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--bg4)">#</th>
        <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--bg4)">Тип</th>
        <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--bg4)">Статус</th>
        <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--bg4)">Результат</th>
      </tr>`;
    (res.results||[]).forEach(r => {
      const statusIcon = r.status === "executed" ? '<span style="color:var(--green)">&#10003;</span>' :
        r.status === "skipped" ? '<span style="color:var(--text3)">&#8212;</span>' :
        r.status === "error" ? '<span style="color:var(--red)">&#10007;</span>' :
        '<span style="color:var(--accent)">&#9672;</span>';
      html += `<tr>
        <td style="padding:6px 8px;border-bottom:1px solid var(--bg4)">${r.index+1}</td>
        <td style="padding:6px 8px;border-bottom:1px solid var(--bg4)"><span class="action-badge ${r.type}">${r.type}</span></td>
        <td style="padding:6px 8px;border-bottom:1px solid var(--bg4)">${statusIcon} ${r.status}</td>
        <td style="padding:6px 8px;border-bottom:1px solid var(--bg4);font-family:monospace;font-size:11px">${escHtml(r.output||'')}</td>
      </tr>`;
    });
    html += `</table>`;
    results.innerHTML = html;
    panel.style.display = "block";
    panel.scrollIntoView({behavior:'smooth'});
  }catch(e){
    showToast("Ошибка: "+e.message, "error");
  }
  btn.disabled = false;
  btn.innerHTML = '<i class="fas fa-flask"></i> Тест';
}

/* ====== Direct Install (Phase 4d) ====== */
async function generateAndInstallDirect(){
  // Generate without redirect, show result in place
  const config = buildConfig();
  try{
    const r = await fetch("/api/extensions/generate-visual", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
    const result = await r.json();
    if(result.ok){
      showToast("Расширение установлено!", "success");
    } else {
      showToast("Ошибка: "+ (result.error||"неизвестная"), "error");
    }
  }catch(e){
    showToast("Ошибка: "+e.message, "error");
  }
}

/* ====== First Extension Tutorial / Tour ====== */
const TOUR_STEPS = [
  {
    title: "Добро пожаловать в конструктор расширений!",
    text: "Я проведу вас через создание вашего первого расширения. Это займёт всего минуту.\n\nВы узнаете как:\n• Выбрать шаблон\n• Настроить модуль и хуки\n• Создать работающее расширение",
    selector: null,
    step: null,
    btn: "Начать тур →"
  },
  {
    title: "Шаг 1: Выберите шаблон",
    text: "Нажмите на шаблон «Минимальное» — он создаст простое расширение с логированием.\n\nВы всегда сможете изменить настройки позже.",
    selector: ".template-card",
    step: 0,
    highlight: true,
    autoAction: "applyTemplate('minimal')",
    btn: "Применить шаблон"
  },
  {
    title: "Шаг 2: Выберите модуль",
    text: "Выберите модуль «Все модули» (он выделен синим). Это позволит расширению работать везде.\n\nВы также можете привязать расширение к конкретному модулю: Книги, Медиа, Поиск и т.д.",
    selector: ".module-card[onclick*='all']",
    step: 1,
    highlight: true,
    autoAction: "toggleModule('all')",
    btn: "Выбрать все модули"
  },
  {
    title: "Шаг 3: Действия на хуки",
    text: "Отлично! Теперь выберите событие «on_load» и посмотрите как работают действия.\n\nУ каждого хука можно настроить несколько действий: логирование, уведомления, API-запросы, изменение интерфейса и другие.",
    selector: ".hook-item",
    step: 2,
    highlight: true
  },
  {
    title: "Шаг 4: Создание расширения",
    text: "Мы почти у цели! Перейдите на последний шаг, проверьте конфигурацию и нажмите «Создать расширение».\n\nРасширение будет установлено и появится в списке расширений.",
    selector: "#genBtn",
    step: 7,
    highlight: true
  },
  {
    title: "Поздравляю! 🎉",
    text: "Вы создали своё первое расширение!\n\n• Оно появится в разделе «Расширения» на странице /apps\n• Там вы можете включать/отключать его\n• Возвращайтесь в конструктор чтобы создавать новые расширения",
    selector: null,
    step: null,
    btn: "Завершить"
  }
];

let tourActive = false;
let tourStep = -1;

function shouldStartTour(){
  try { return !localStorage.getItem("ext_tour_done"); } catch(e){ return false; }
}

function completeTour(){
  tourActive = false;
  tourStep = -1;
  try { localStorage.setItem("ext_tour_done", "1"); } catch(e){}
  const overlay = document.getElementById("tourOverlay");
  if(overlay) overlay.remove();
}

function startTour(){
  tourActive = true;
  tourStep = -1;
  nextTourStep();
}

function skipTour(){
  completeTour();
  showToast("Тур пропущен. Вы всегда можете начать заново, очистив localStorage", "info");
}

function nextTourStep(){
  tourStep++;
  if(tourStep >= TOUR_STEPS.length){
    completeTour();
    return;
  }
  renderTourStep(tourStep);
}

function prevTourStep(){
  if(tourStep <= 0) return;
  tourStep--;
  renderTourStep(tourStep);
}

function renderTourStep(idx){
  const s = TOUR_STEPS[idx];
  if(!s) return;

  // Navigate to correct step if specified
  if(s.step !== null && s.step !== undefined){
    showStep(s.step);
  }

  // Remove existing overlay
  const old = document.getElementById("tourOverlay");
  if(old) old.remove();

  // Create overlay
  const overlay = document.createElement("div");
  overlay.id = "tourOverlay";
  overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;pointer-events:none";

  // Find target element for highlighting
  let targetRect = null;
  if(s.selector){
    const el = document.querySelector(s.selector);
    if(el) targetRect = el.getBoundingClientRect();
  }

  // Semi-transparent backdrop (only if no highlight target, or around the target)
  const backdrop = document.createElement("div");
  backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.55);z-index:9999;pointer-events:auto";

  if(targetRect && s.highlight){
    // Cut out the target area
    const padding = 8;
    const cutout = document.createElement("div");
    cutout.style.cssText = `position:fixed;left:${targetRect.left-padding}px;top:${targetRect.top-padding}px;width:${targetRect.width+padding*2}px;height:${targetRect.height+padding*2}px;z-index:10000;pointer-events:none;border-radius:8px;box-shadow:0 0 0 9999px rgba(0,0,0,0.55);`;
    backdrop.appendChild(cutout);

    // Pulse animation on target
    const pulse = document.createElement("div");
    pulse.style.cssText = `position:fixed;left:${targetRect.left-4}px;top:${targetRect.top-4}px;width:${targetRect.width+8}px;height:${targetRect.height+8}px;z-index:10001;pointer-events:none;border:2px solid var(--accent,#6366f1);border-radius:8px;animation:tourPulse 1.5s ease-in-out infinite`;
    overlay.appendChild(pulse);
  }

  overlay.appendChild(backdrop);

  // Tooltip card
  const tooltip = document.createElement("div");
  tooltip.style.cssText = `position:fixed;z-index:10002;pointer-events:auto;background:#1a1a2e;border:1px solid var(--accent,#6366f1);border-radius:12px;padding:18px 22px;max-width:420px;width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.5)`;

  const isFirst = idx === 0;
  const isLast = idx === TOUR_STEPS.length - 1;

  // Position tooltip
  if(targetRect){
    const top = targetRect.bottom + 20;
    const left = Math.max(16, Math.min(targetRect.left + targetRect.width/2 - 200, window.innerWidth - 416));
    tooltip.style.top = Math.min(top, window.innerHeight - 280) + "px";
    tooltip.style.left = left + "px";
  } else if(isFirst || isLast){
    tooltip.style.top = "50%";
    tooltip.style.left = "50%";
    tooltip.style.transform = "translate(-50%,-50%)";
    tooltip.style.textAlign = "center";
  } else {
    tooltip.style.top = "50%";
    tooltip.style.left = "50%";
    tooltip.style.transform = "translate(-50%,-50%)";
  }

  // Progress dots
  const dots = TOUR_STEPS.map((_, i) =>
    `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 3px;background:${i===idx?'var(--accent,#6366f1)':'var(--bg4,#2d2d3d)'};transition:var(--tr)"></span>`
  ).join("");

  let buttons = "";
  if(isFirst){
    buttons = `
      <button onclick="skipTour()" style="background:none;border:none;color:var(--text3,#888);cursor:pointer;font-size:12px;padding:6px 12px">Пропустить</button>
      <button onclick="nextTourStep()" style="padding:8px 24px;border-radius:8px;border:none;background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff;cursor:pointer;font-weight:600;font-size:13px">${s.btn||'Далее →'}</button>`;
  } else if(isLast){
    buttons = `<button onclick="completeTour()" style="padding:8px 24px;border-radius:8px;border:none;background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff;cursor:pointer;font-weight:600;font-size:13px">${s.btn||'Завершить'}</button>`;
  } else {
    const autoBtn = s.autoAction ? `<button onclick="${s.autoAction};nextTourStep()" style="padding:6px 16px;border-radius:6px;border:none;background:var(--accent,#6366f1);color:#fff;cursor:pointer;font-weight:500;font-size:12px">${s.btn||'Выполнить'}</button>` : '';
    buttons = `
      <button onclick="skipTour()" style="background:none;border:none;color:var(--text3,#888);cursor:pointer;font-size:12px;padding:6px 10px">Пропустить</button>
      ${autoBtn}
      <button onclick="nextTourStep()" style="padding:6px 18px;border-radius:6px;border:none;background:var(--accent,#6366f1);color:#fff;cursor:pointer;font-weight:500;font-size:12px">${s.btn||'Далее →'}</button>`;
  }

  tooltip.innerHTML = `
    <div style="font-size:14px;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:8px">${isFirst||isLast?'':'<span style="background:var(--accent,#6366f1);color:#fff;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700">'+(idx+1)+'</span>'}${s.title}</div>
    <div style="font-size:12px;color:var(--text2,#aaa);line-height:1.6;margin-bottom:12px;white-space:pre-line">${s.text}</div>
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
      <div>${dots}</div>
      <div style="display:flex;gap:6px;align-items:center">${buttons}</div>
    </div>`;

  overlay.appendChild(tooltip);
  document.body.appendChild(overlay);
}

// Inject tour pulse keyframe
(function(){
  const style = document.createElement("style");
  style.textContent = `@keyframes tourPulse{0%,100%{box-shadow:0 0 0 0 rgba(99,102,241,0.4)}50%{box-shadow:0 0 0 8px rgba(99,102,241,0)}}`;
  document.head.appendChild(style);
})();

// Override init to add tour trigger
const _origInit = init;
init = function(){
  _origInit();
  // Check if tour should start after page loads
  setTimeout(() => {
    const forceTour = window.location.search.includes("tour=1");
    if(forceTour){
      try { localStorage.removeItem("ext_tour_done"); } catch(e){}
    }
    if((shouldStartTour() || forceTour) && state && state.name === ""){
      startTour();
    }
  }, 800);
};

init();

/* ===== Module help modal (CSS selectors + API reference) ===== */
let helpModule = "all";
let helpTab = "css";

function openModuleHelp(mod){
  helpModule = mod || getTargetModule() || "all";
  const modal = document.getElementById("helpModal");
  modal.style.display = "flex";
  renderHelpModFilter();
  const activeTab = document.querySelector(".help-tab.active") || document.querySelector(".help-tab");
  if(activeTab) setHelpTab(activeTab.getAttribute("data-tab"), activeTab);
  renderHelpBody();
}
function closeModuleHelp(){
  const modal = document.getElementById("helpModal");
  if(modal) modal.style.display = "none";
}
function renderHelpModFilter(){
  const mods = (data && data.modules) ? data.modules : [];
  const cont = document.getElementById("helpModFilter");
  if(!cont) return;
  cont.innerHTML = mods.map(m => {
    const label = (data.module_info||{})[m] || m;
    const active = m === helpModule ? " active" : "";
    return `<button class="api-mod-filter-btn${active}" onclick="setHelpModule('${m}',this)">${label}</button>`;
  }).join("");
}
function setHelpModule(m, btn){
  helpModule = m;
  document.querySelectorAll("#helpModFilter .api-mod-filter-btn").forEach(b => b.classList.remove("active"));
  if(btn) btn.classList.add("active");
  renderHelpBody();
}
function setHelpTab(tab, btn){
  helpTab = tab;
  document.querySelectorAll(".help-tab").forEach(b => b.classList.remove("active"));
  if(btn) btn.classList.add("active");
  const cssP = document.getElementById("helpTabCss");
  const apiP = document.getElementById("helpTabApi");
  if(cssP) cssP.style.display = (tab === "css") ? "block" : "none";
  if(apiP) apiP.style.display = (tab === "api") ? "block" : "none";
  renderHelpBody();
}
function renderHelpBody(){
  const modLabel = (data && data.module_info && data.module_info[helpModule]) || helpModule;
  const cssRef = (data && data.css_reference && data.css_reference[helpModule]) || null;
  let apiRef = [];
  if(helpModule === "all"){
    const ar = (data && data.api_reference) || {};
    for(const k in ar){ apiRef = apiRef.concat(ar[k] || []); }
  } else {
    apiRef = (data && data.api_reference && data.api_reference[helpModule]) || [];
  }

  const cssBox = document.getElementById("helpTabCss");
  if(cssBox){
    if(cssRef && cssRef.selectors && cssRef.selectors.length){
      let html = '<div class="ref-list">';
      html += cssRef.selectors.map(s => {
        const kind = (s.kind === "id") ? "id" : (s.kind === "class" ? "class" : "attr");
        const desc = s.description || "";
        return `<div class="ref-item"><code class="ref-sel">${escHtml(s.selector)}</code><span class="ref-kind ${kind}">${kind}</span><span class="ref-desc">${escHtml(desc)}</span></div>`;
      }).join("");
      html += "</div>";
      if(cssRef.notes){
        html += `<div class="ref-notes"><b>Динамические элементы:</b><br>${escHtml(cssRef.notes)}</div>`;
      }
      cssBox.innerHTML = html;
    } else {
      cssBox.innerHTML = `<div class="api-empty-state"><p>Нет данных о CSS-селекторах для модуля «${escHtml(modLabel)}».</p></div>`;
    }
  }

  const apiBox = document.getElementById("helpTabApi");
  if(apiBox){
    if(apiRef && apiRef.length){
      let html = '<div class="ref-list">';
      html += apiRef.map(e => {
        const d = e.description || "";
        return `<div class="ref-item"><span class="method-tag ${escHtml((e.method||'GET').toLowerCase())}">${escHtml(e.method||'GET')}</span><code class="ref-sel">${escHtml(e.path||'')}</code><span class="ref-desc">${escHtml(d)}</span></div>`;
      }).join("");
      html += "</div>";
      apiBox.innerHTML = html;
    } else {
      apiBox.innerHTML = `<div class="api-empty-state"><p>Нет данных об API для модуля «${escHtml(modLabel)}».</p></div>`;
    }
  }
}
</script>

<button class="help-fab" onclick="openModuleHelp()" title="Справочник модуля (CSS-селекторы и API)">?</button>

<div class="help-modal" id="helpModal">
  <div class="help-modal-overlay" onclick="closeModuleHelp()"></div>
  <div class="help-modal-content">
    <div class="help-modal-header">
      <h3><i class="fas fa-book" style="color:var(--accent)"></i> Справочник модуля</h3>
      <button class="help-close" onclick="closeModuleHelp()">&times;</button>
    </div>
    <div class="help-mod-filter" id="helpModFilter"></div>
    <div class="help-tabs">
      <button class="help-tab active" data-tab="css" onclick="setHelpTab('css',this)">CSS селекторы</button>
      <button class="help-tab" data-tab="api" onclick="setHelpTab('api',this)">API методы</button>
    </div>
    <div class="help-body">
      <div id="helpTabCss" class="help-tab-panel"></div>
      <div id="helpTabApi" class="help-tab-panel" style="display:none"></div>
    </div>
  </div>
</div>

</body>
</html>
'''
