#!C:/ProgramData/Anaconda3/python.exe
# -*- coding: utf-8 -*-
"""Extension Edit Page — файловый менеджер + редактор кода + терминал + сервер."""

PAGE_EXT_EDIT = r"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Редактор расширения</title>
<link rel="stylesheet" href="/static/css/font-awesome.min.css">
<link rel="stylesheet" href="/static/fonts/google-fonts.css">
<link rel="stylesheet" href="/static/libs/codemirror.min.css">
<link rel="stylesheet" href="/static/libs/cm-theme-dark.min.css">
<link rel="stylesheet" href="/static/libs/dialog.css">
<link rel="stylesheet" href="/static/libs/xterm.min.css">
<script src="/static/libs/codemirror.min.js"></script>
<script src="/static/libs/cm-mode-xml.min.js"></script>
<script src="/static/libs/cm-mode-js.min.js"></script>
<script src="/static/libs/cm-mode-css.min.js"></script>
<script src="/static/libs/cm-mode-html.min.js"></script>
<script src="/static/libs/cm-mode-python.min.js"></script>
<script src="/static/libs/cm-closebrackets.min.js"></script>
<script src="/static/libs/cm-matchbrackets.min.js"></script>
<script src="/static/libs/search.min.js"></script>
<script src="/static/libs/searchcursor.min.js"></script>
<script src="/static/libs/dialog.min.js"></script>
<script src="/static/libs/xterm.min.js"></script>
<script src="/static/libs/xterm-addon-fit.min.js"></script>
<script src="/static/libs/jszip.min.js"></script>
<style>
:root{--bg:#0f1419;--bg2:#151b23;--bg3:#1c242f;--bg4:#232d3a;--border:rgba(255,255,255,0.08);--border2:rgba(255,255,255,0.12);--text:#e4e8ef;--text2:#a0aec0;--text3:#6b7280;--accent:#6366f1;--accent2:#4f46e5;--accent-glow:rgba(99,102,241,0.25);--accent-light:rgba(99,102,241,0.15);--green:#10b981;--red:#ef4444;--orange:#f97316;--purple:#a855f7;--glass:rgba(21,27,35,0.6);--glass-border:rgba(255,255,255,0.10);--hover:rgba(255,255,255,0.05);--shadow-sm:0 2px 8px rgba(0,0,0,0.2);--shadow:0 4px 20px rgba(0,0,0,0.3);--shadow-lg:0 8px 40px rgba(0,0,0,0.4);--radius:14px;--radius-sm:8px;--radius-lg:20px;--radius-xl:32px;--tr:all 0.25s cubic-bezier(0.4,0,0.2,1)}
body.light{--bg:#fafbfc;--bg2:#fff;--bg3:#f3f4f6;--bg4:#e5e7eb;--border:rgba(0,0,0,0.06);--border2:rgba(0,0,0,0.10);--text:#111827;--text2:#4b5563;--text3:#9ca3af;--accent:#4f46e5;--accent2:#4338ca;--accent-glow:rgba(79,70,229,0.2);--accent-light:rgba(79,70,229,0.1);--green:#059669;--red:#dc2626;--orange:#d97706;--purple:#7c3aed;--glass:rgba(255,255,255,0.72);--glass-border:rgba(0,0,0,0.08);--hover:rgba(0,0,0,0.04);--shadow-sm:0 2px 8px rgba(0,0,0,0.04);--shadow:0 4px 20px rgba(0,0,0,0.06);--shadow-lg:0 8px 40px rgba(0,0,0,0.08)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Inter,system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden;position:relative}
body::before{content:'';position:fixed;inset:0;pointer-events:none;background:radial-gradient(ellipse 80% 50% at 20% -10%,var(--accent-glow),transparent),radial-gradient(ellipse 60% 40% at 85% 85%,rgba(168,85,247,0.06),transparent);z-index:0}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:var(--accent)}
*{scrollbar-width:thin;scrollbar-color:var(--bg4) transparent}

/* Toolbar */
.toolbar{position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:1000;background:var(--glass);backdrop-filter:blur(28px) saturate(180%);-webkit-backdrop-filter:blur(28px) saturate(180%);border:1px solid var(--glass-border);border-radius:var(--radius-xl);padding:6px 8px;display:flex;align-items:center;gap:4px;box-shadow:var(--shadow-lg);max-width:calc(100vw - 24px);overflow-x:auto;white-space:nowrap;flex-shrink:0}
.toolbar::-webkit-scrollbar{display:none}
.toolbar-title{font-size:14px;font-weight:700;color:var(--text);margin:0 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px;flex-shrink:1}
.toolbar-btn{flex-shrink:0;width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;background:transparent;border:1px solid transparent;border-radius:var(--radius-sm);color:var(--text2);cursor:pointer;font-size:13px;transition:var(--tr);text-decoration:none}
.toolbar-btn:hover{background:var(--hover);color:var(--text);border-color:var(--border);transform:translateY(-1px)}
.toolbar-btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff!important;border-color:transparent;box-shadow:0 2px 8px var(--accent-glow)}
.toolbar-btn-primary:hover{box-shadow:0 4px 16px var(--accent-glow);transform:translateY(-2px)}
.toolbar-btn-danger{color:var(--red)!important}
.toolbar-btn-danger:hover{background:rgba(239,68,68,0.1);border-color:var(--red)}
.toolbar-sep{width:1px;height:20px;flex-shrink:0;background:linear-gradient(to bottom,transparent,var(--border2),transparent);margin:0 2px}
.status-pill{padding:3px 10px;background:var(--bg3);border:1px solid var(--border);border-radius:20px;font-size:10px;color:var(--text2);font-weight:600;cursor:pointer;flex-shrink:0;white-space:nowrap;display:inline-flex;align-items:center;gap:4px;transition:var(--tr)}
.status-pill.running{color:var(--green);background:rgba(16,185,129,0.1);border-color:rgba(16,185,129,0.3)}
.status-pill.stopped{color:var(--red);background:rgba(239,68,68,0.1);border-color:rgba(239,68,68,0.3)}
.status-pill:hover{transform:translateY(-1px)}

/* Main layout */
.main{flex:1;display:flex;overflow:hidden;padding-top:64px;position:relative;z-index:1}

/* Sidebar */
.sidebar{width:240px;background:var(--glass);backdrop-filter:blur(16px) saturate(180%);-webkit-backdrop-filter:blur(16px) saturate(180%);border-right:1px solid var(--glass-border);display:flex;flex-direction:column;flex-shrink:0}
.sidebar-header{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:4px}
.sidebar-header button{padding:4px 8px;background:transparent;border:1px solid transparent;border-radius:var(--radius-sm);color:var(--text2);cursor:pointer;font-size:12px;transition:var(--tr);display:flex;align-items:center;gap:4px}
.sidebar-header button:hover{background:var(--hover);color:var(--text);border-color:var(--border)}
.file-tree{flex:1;overflow-y:auto;padding:4px 0}
.file-item{display:flex;align-items:center;gap:8px;padding:5px 12px;font-size:13px;cursor:pointer;transition:var(--tr);border-radius:0;margin:0 4px;border-radius:6px}
.file-item:hover{background:var(--hover)}
.file-item.active{background:var(--accent-light);color:var(--accent);font-weight:600}
.file-item i{width:16px;text-align:center;color:var(--orange);font-size:12px;flex-shrink:0}
.file-item .del{margin-left:auto;opacity:0;transition:opacity .15s}
.file-item:hover .del{opacity:1}
.file-item .del:hover{color:var(--red)}

/* Editor area */
.editor-area{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg2)}
.editor-tabs{display:flex;background:var(--bg3);border-bottom:1px solid var(--border);overflow-x:auto;flex-shrink:0}
.editor-tab{padding:5px 14px;font-size:12px;background:transparent;border-right:1px solid var(--border);cursor:pointer;color:var(--text2);display:flex;align-items:center;gap:6px;white-space:nowrap;transition:var(--tr)}
.editor-tab:hover{background:var(--hover);color:var(--text)}
.editor-tab.active{background:var(--bg);color:var(--text);box-shadow:inset 0 -2px 0 var(--accent)}
.editor-tab .close{padding:2px 4px;border-radius:3px;font-size:10px;margin-left:2px}
.editor-tab .close:hover{background:var(--red);color:#fff}
.editor-tabs .add-tab{flex-shrink:0;width:28px;height:28px;display:flex;align-items:center;justify-content:center;background:transparent;border:none;color:var(--text3);cursor:pointer;font-size:12px;transition:var(--tr)}
.editor-tabs .add-tab:hover{color:var(--accent)}
#editorContainer{flex:1;min-height:0}
.CodeMirror{height:100%!important;font-size:14px}

/* Resizer */
.resizer{width:5px;cursor:col-resize;flex-shrink:0;background:transparent;position:relative;z-index:10;transition:background .15s}
.resizer::after{content:'';position:absolute;top:4px;left:2px;right:2px;bottom:4px;background:var(--bg4);border-radius:3px;transition:background .15s}
.resizer:hover::after,.resizer.active::after{background:var(--accent);width:3px;left:1px;right:1px}

/* Preview */
.preview-area{width:50%;border-left:1px solid var(--glass-border);display:none;flex-direction:column;flex-shrink:0;background:var(--glass);backdrop-filter:blur(12px)}
.preview-area.show{display:flex}
.preview-area iframe{flex:1;border:none;background:#fff}
.preview-header{padding:6px 12px;background:var(--bg3);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:12px}
.preview-header .title{font-size:12px;font-weight:600;color:var(--text2)}
.preview-header button{padding:4px 8px;background:transparent;border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text2);cursor:pointer;font-size:11px;transition:var(--tr)}
.preview-header button:hover{background:var(--hover);color:var(--text);border-color:var(--accent)}

/* Terminal panel */
.terminal-panel{height:200px;border-top:1px solid var(--border);display:none;flex-direction:column;background:var(--bg)}
.terminal-panel.show{display:flex}
.terminal-header{padding:4px 12px;background:var(--bg3);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;font-size:12px}
.terminal-header button{padding:2px 8px;background:transparent;border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text2);cursor:pointer;font-size:11px;transition:var(--tr)}
.terminal-header button:hover{background:var(--hover);color:var(--text)}
#terminal{flex:1;overflow:hidden;padding:4px 0}
.xterm{padding:4px}

/* AI Chat Panel */
.ai-panel{width:300px;background:var(--glass);backdrop-filter:blur(16px) saturate(180%);-webkit-backdrop-filter:blur(16px) saturate(180%);border-left:1px solid var(--glass-border);display:none;flex-direction:column;flex-shrink:0}
.ai-panel.show{display:flex}
.ai-panel-header{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}
.ai-panel-header .ai-icon{color:var(--accent);font-size:14px}
.ai-panel-header button{margin-left:auto;background:none;border:none;color:var(--text2);cursor:pointer;padding:4px;border-radius:var(--radius-sm);transition:var(--tr)}
.ai-panel-header button:hover{background:var(--hover);color:var(--text)}
.ai-panel-header select{padding:4px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:11px;cursor:pointer;max-width:130px;outline:none;transition:var(--tr)}
.ai-panel-header select:hover{border-color:var(--accent)}
.ai-messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
.ai-msg-user{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;padding:8px 12px;border-radius:12px 12px 0 12px;max-width:85%;margin-left:auto;font-size:13px;line-height:1.5;animation:msgIn .3s cubic-bezier(0.34,1.56,0.64,1);box-shadow:0 4px 16px var(--accent-glow)}
.ai-msg-assistant{background:var(--glass);backdrop-filter:blur(12px);padding:10px 14px;border-radius:12px 12px 12px 0;max-width:100%;font-size:13px;line-height:1.6;border:1px solid var(--glass-border);animation:msgIn .3s cubic-bezier(0.34,1.56,0.64,1)}
@keyframes msgIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideInRight{from{transform:translateX(40px);opacity:0}to{transform:translateX(0);opacity:1}}
.ai-thinking{display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--glass);backdrop-filter:blur(12px);border-radius:12px 12px 12px 0;border:1px solid var(--glass-border);font-size:13px;color:var(--text2);animation:msgIn .3s ease}
.ai-thinking .spinner{width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.ai-input-area{padding:8px 10px;border-top:1px solid var(--border);display:flex;gap:6px}
.ai-input-area input{flex:1;padding:9px 14px;background:var(--bg);border:1.5px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:13px;outline:none;transition:var(--tr)}
.ai-input-area input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-light)}
.ai-send-btn{width:38px;height:38px;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:var(--radius-sm);color:#fff;cursor:pointer;font-size:14px;transition:var(--tr);box-shadow:0 2px 8px var(--accent-glow);flex-shrink:0}
.ai-send-btn:hover{transform:scale(1.05);box-shadow:0 4px 16px var(--accent-glow)}
.ai-btn-group{display:flex;gap:4px;margin-bottom:4px}
.ai-btn-group button{flex:1;padding:5px;border-radius:var(--radius-sm);font-size:11px;cursor:pointer;transition:var(--tr);display:flex;align-items:center;justify-content:center;gap:4px}
.ai-btn-group .btn-apply{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;box-shadow:0 2px 8px var(--accent-glow)}
.ai-btn-group .btn-apply:hover{transform:translateY(-1px);box-shadow:0 4px 12px var(--accent-glow)}
.ai-btn-group .btn-insert{background:var(--bg3);border:1px solid var(--border);color:var(--text)}
.ai-btn-group .btn-insert:hover{background:var(--hover);border-color:var(--accent);color:var(--accent)}

/* Console tab bar */
.panel-tab{padding:2px 10px;font-size:12px;cursor:pointer;color:var(--text2);border-radius:6px;display:flex;align-items:center;gap:4px;transition:var(--tr)}
.panel-tab.active{background:var(--bg);color:var(--text)}
.panel-tab:hover{color:var(--text)}
.console-panel{flex:1;overflow-y:auto;padding:4px 0;font-family:JetBrains Mono,monospace;font-size:12px;display:none;flex-direction:column}
.console-panel.show{display:flex}
.console-entry{padding:3px 10px;border-bottom:1px solid var(--border);line-height:1.5;white-space:pre-wrap;word-break:break-all;font-size:11px}
.console-entry .lvl{display:inline-block;padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700;margin-right:6px;text-transform:uppercase}
.console-entry.log .lvl,.console-entry.info .lvl{background:var(--accent-light);color:var(--accent)}
.console-entry.warn .lvl{background:rgba(249,115,22,0.15);color:var(--orange)}
.console-entry.error .lvl{background:rgba(239,68,68,0.15);color:var(--red)}
.console-entry.warn{background:rgba(249,115,22,0.04)}
.console-entry.error{background:rgba(239,68,68,0.04)}
.console-entry .time{color:var(--text3);font-size:10px;margin-right:6px}
.console-entry .msg{color:var(--text)}

/* Modal */
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(8px);z-index:2000;display:none;align-items:center;justify-content:center}
.modal-overlay.active{display:flex}
.modal-box{background:var(--bg2);border:1px solid var(--glass-border);border-radius:var(--radius-lg);padding:24px 28px;max-width:520px;width:92%;box-shadow:var(--shadow-lg);animation:modalIn .35s cubic-bezier(0.34,1.56,0.64,1);max-height:85vh;overflow-y:auto}
@keyframes modalIn{from{opacity:0;transform:scale(0.92) translateY(16px)}to{opacity:1;transform:scale(1) translateY(0)}}
.modal-box h3{font-size:18px;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.modal-box label{display:block;font-size:12px;color:var(--text2);margin-bottom:5px;font-weight:600}
.modal-box input,.modal-box textarea,.modal-box select{width:100%;padding:9px 12px;background:var(--bg);border:1.5px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:13px;outline:none;transition:var(--tr)}.modal-box input:focus,.modal-box textarea:focus,.modal-box select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-light)}
.modal-box .modal-actions{display:flex;gap:8px;margin-top:16px}
.modal-box .modal-actions button{flex:1;padding:9px;border-radius:var(--radius-sm);font-size:13px;font-weight:600;cursor:pointer;transition:var(--tr)}
.modal-box .modal-actions .btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;box-shadow:0 2px 8px var(--accent-glow)}
.modal-box .modal-actions .btn-primary:hover{transform:translateY(-1px);box-shadow:0 4px 16px var(--accent-glow)}
.modal-box .modal-actions .btn-cancel{background:var(--bg3);border:1px solid var(--border);color:var(--text)}
.modal-box .modal-actions .btn-cancel:hover{background:var(--hover)}
/* Side panel variant */
.modal-overlay.side-panel{align-items:stretch;justify-content:flex-end;background:rgba(0,0,0,0.2);backdrop-filter:blur(2px)}
.modal-overlay.side-panel .modal-box{max-width:340px;width:340px;max-height:100vh;border-radius:0;border:none;padding:14px 10px;animation:slideInRight .3s ease;margin:0;box-shadow:-8px 0 32px rgba(0,0,0,0.3)}
.modal-overlay.side-panel .modal-box h3{font-size:14px;margin-bottom:10px}
.modal-overlay.side-panel .modal-box label{font-size:10px;margin-bottom:3px}
.modal-overlay.side-panel .modal-box input,.modal-overlay.side-panel .modal-box select{padding:5px 8px;font-size:11px}
.modal-overlay.side-panel .modal-actions{margin-top:10px}
.modal-overlay.side-panel .modal-actions button{padding:7px;font-size:11px}
#visLiveBtn{background:var(--bg3);border:1px solid var(--border);color:var(--text);cursor:pointer;font-size:11px;padding:5px 8px;border-radius:6px;white-space:nowrap}
#visLiveBtn:hover{background:var(--hover)}
.vis-num-mode input[type=range]{display:none}
.vis-num-mode .vis-slider-val{display:none}
.vis-num-mode input.vis-num-input{display:inline-block!important;width:50px!important;padding:2px 4px!important;font-size:10px!important}
input.vis-num-input{display:none}
.vis-slider-group{display:flex;align-items:center;gap:4px}
.vis-slider-group input[type=range]{flex:1;min-width:0}
.vis-slider-group .vis-slider-val{font-size:10px;min-width:18px;text-align:center}

/* Toast */
.toast-container{position:fixed;bottom:30px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{padding:10px 20px;border-radius:var(--radius-sm);font-size:13px;font-weight:600;background:var(--glass);backdrop-filter:blur(20px) saturate(180%);border:1px solid var(--glass-border);border-left:3px solid var(--accent);color:var(--text);box-shadow:var(--shadow);animation:toastIn .3s cubic-bezier(0.34,1.56,0.64,1);pointer-events:auto}
.toast.success{border-left-color:var(--green)}
.toast.error{border-left-color:var(--red)}
@keyframes toastIn{from{opacity:0;transform:translateY(20px) scale(0.95)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}

/* Preview fullscreen */
body.preview-fs .toolbar{display:none!important}
body.preview-fs .sidebar,body.preview-fs .editor-area,body.preview-fs .ai-panel,body.preview-fs .config-panel,body.preview-fs .resizer{display:none!important}
body.preview-fs .preview-area{position:fixed;top:0;left:0;right:0;bottom:0;z-index:900;width:100%!important;height:100vh!important;border:none!important;border-radius:0!important;background:var(--bg)!important;display:flex!important}
body.preview-fs .preview-area .preview-header{padding:8px 14px;flex-shrink:0}
body.preview-fs .preview-area iframe{flex:1;height:100%!important}
body.preview-fs .preview-area #inspectInfo{flex-shrink:0}

/* Inline code blocks in AI messages */
.code-block-wrapper{position:relative;margin:8px 0;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden;background:var(--bg)}
.code-block-header{display:flex;gap:4px;justify-content:flex-end;padding:4px 8px;background:var(--bg3);border-bottom:1px solid var(--border)}
.code-block-header button{padding:2px 8px;background:transparent;border:1px solid transparent;border-radius:4px;color:var(--text2);cursor:pointer;font-size:10px;transition:var(--tr)}
.code-block-header button:hover{background:var(--hover);color:var(--text);border-color:var(--border)}
.code-block-content{background:var(--bg);padding:10px 14px;overflow-x:auto;font-size:12px;font-family:JetBrains Mono,monospace;line-height:1.5}
</style>
<script src="/static/js/i18n.js"></script>
</head><body>
<div class="toolbar" id="toolbar">
  <a href="/apps" class="toolbar-btn" title="Назад"><i class="fas fa-arrow-left"></i></a>
  <span class="toolbar-title" id="extTitle">Редактор</span>
  <span class="status-pill stopped" id="reloadBtn" onclick="reloadExtension()" title="Перезагрузить расширение"><i class="fas fa-sync-alt"></i> Перезагрузить</span>
  <span class="toolbar-sep"></span>
  <button class="toolbar-btn" onclick="openExtSettings()" title="Настройки"><i class="fas fa-cog"></i></button>
  <button class="toolbar-btn" onclick="togglePreview()" title="Превью"><i class="fas fa-eye"></i></button>
  <button class="toolbar-btn" onclick="toggleTerminal()" title="Терминал"><i class="fas fa-terminal"></i></button>
  <button class="toolbar-btn" onclick="toggleEditorType()" title="Редактор"><i class="fas fa-code"></i></button>
  <button class="toolbar-btn" onclick="toggleAiChat()" title="AI"><i class="fas fa-robot"></i></button>
  <button class="toolbar-btn" onclick="openInDesktop()" title="Desktop"><i class="fas fa-desktop"></i></button>
  <span class="toolbar-sep"></span>
  <button class="toolbar-btn" onclick="beautifyCode()" title="Beautifier (HTML/JS/CSS/JSON)"><i class="fas fa-magic"></i></button>
  <button class="toolbar-btn" onclick="editorSearch()" title="Поиск (Ctrl+F)"><i class="fas fa-search"></i></button>
  <button class="toolbar-btn" onclick="exportZip()" title="Экспорт .zip"><i class="fas fa-file-zipper"></i></button>
  <button class="toolbar-btn toolbar-btn-primary" onclick="saveCurrentFile()" title="Сохранить (Ctrl+S)"><i class="fas fa-save"></i></button>
  <button class="toolbar-btn toolbar-btn-danger" onclick="deleteCurrentFile()" title="Удалить"><i class="fas fa-trash"></i></button>
  <span class="toolbar-sep"></span>
  <button class="toolbar-btn" id="themeBtn" onclick="toggleTheme()" title="Тема"><i class="fas fa-moon"></i></button>
</div>
<div class="main" id="mainContainer">
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <span style="flex:1;font-size:12px;font-weight:600">Файлы</span>
      <button onclick="createNewFile()" title="Новый файл"><i class="fas fa-file-circle-plus"></i></button>
      <button onclick="createNewFolder()" title="Новая папка"><i class="fas fa-folder-circle-plus"></i></button>
      <button onclick="loadFiles()" title="Обновить"><i class="fas fa-rotate-right"></i></button>
    </div>
    <div class="file-tree" id="fileTree"></div>
  </div>
  <div class="resizer" id="resizerSidebar" data-target="sidebar" data-dir="1"></div>
  <div class="editor-area" id="editorArea">
    <div class="editor-tabs" id="editorTabs"></div>
    <div id="editorContainer"></div>
    <div class="terminal-panel" id="terminalPanel">
      <div class="terminal-header">
        <div style="display:flex;gap:2px">
          <span class="panel-tab active" data-panel="term" onclick="switchPanelTab('term')"><i class="fas fa-terminal"></i> Терминал</span>
          <span class="panel-tab" data-panel="console" onclick="switchPanelTab('console')"><i class="fas fa-list"></i> Консоль</span>
        </div>
        <div style="display:flex;gap:4px;align-items:center">
          <button id="consoleClearBtn" onclick="clearConsole()" style="display:none;font-size:10px" title="Очистить консоль"><i class="fas fa-trash-alt"></i></button>
          <button onclick="toggleTerminal()"><i class="fas fa-times"></i></button>
        </div>
      </div>
      <div id="terminal" style="display:flex"></div>
      <div class="console-panel" id="consolePanel"><div style="padding:12px;color:var(--text3);font-size:12px;text-align:center">Консоль расширения. Откройте Превью чтобы увидеть логи.</div></div>
    </div>
  </div>
  <div class="resizer" id="resizerPreview" data-target="previewArea" data-dir="-1" style="display:none"></div>
  <div class="preview-area" id="previewArea">
    <div class="preview-header">
      <span>Превью</span>
      <button id="inspectBtn" onclick="toggleInspector()" title="Инспектор"><i class="fas fa-crosshairs"></i></button>
      <button id="visEditBtn" onclick="toggleVisualEditor()" title="Визуальный редактор"><i class="fas fa-paint-brush"></i></button>
      <button onclick="exportPreviewHtml()" title="Экспорт HTML"><i class="fas fa-file-export"></i></button>
      <button onclick="refreshPreview()"><i class="fas fa-sync"></i></button>
      <button id="fullscreenBtn" onclick="togglePreviewFullscreen()" title="На весь экран"><i class="fas fa-expand"></i></button>
      <button onclick="togglePreview()"><i class="fas fa-times"></i></button>
    </div>
    <div id="inspectInfo" style="display:none;padding:6px 10px;font-size:11px;font-family:JetBrains Mono,monospace;background:var(--bg3);border-bottom:1px solid var(--border);color:var(--text2);max-height:100px;overflow-y:auto"></div>
    <iframe id="previewFrame"></iframe>
  </div>
  <div class="resizer" id="resizerAi" data-target="aiPanel" data-dir="-1" style="display:none"></div>
  <div class="ai-panel" id="aiPanel">
    <div class="ai-panel-header">
      <i class="fas fa-robot ai-icon"></i> AI
      <select id="aiModelSelect" title="Выбрать модель"></select>
      <label style="font-size:11px;display:flex;align-items:center;gap:4px;cursor:pointer;color:var(--text2)" title="Авто-применение кода"><input type="checkbox" id="autoApplyToggle"> Авто</label>
      <button onclick="toggleAiChat()"><i class="fas fa-times"></i></button>
    </div>
    <div class="ai-messages" id="aiMessages"></div>
    <div class="ai-input-area">
      <div class="ai-btn-group">
        <button class="btn-apply" onclick="aiApplyCode()"><i class="fas fa-check"></i> Применить</button>
        <button class="btn-insert" onclick="aiInsertCode()"><i class="fas fa-plus"></i> Вставить</button>
      </div>
      <div style="display:flex;gap:6px">
        <input id="aiInput" placeholder="Спросите AI..." onkeydown="if(event.key==='Enter')aiSend()">
        <button class="ai-send-btn" onclick="aiSend()"><i class="fas fa-paper-plane"></i></button>
      </div>
    </div>
  </div>
  <div class="resizer" id="resizerAi" data-target="aiPanel" data-dir="-1" style="display:none"></div>
</div>

<!-- Ext Settings Modal -->
<div id="extSettingsModal" class="modal-overlay" style="display:none">
  <div class="modal-box" style="max-width:560px">
    <h3><i class="fas fa-cog"></i> Настройки расширения</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;font-size:12px">
      <div><label>Название</label><input id="settingsName"></div>
      <div><label>Версия</label><input id="settingsVersion" placeholder="1.0.0"></div>
      <div style="grid-column:1/-1"><label>Описание</label><textarea id="settingsDesc" rows="2" style="resize:vertical"></textarea></div>
      <div><label>Автор</label><input id="settingsAuthor" placeholder="Автор"></div>
      <div><label>Иконка (Font Awesome)</label><input id="settingsIcon" placeholder="fa-puzzle-piece"></div>
      <div><label>Целевой модуль</label><select id="settingsTargetModule"><option value="all">Все модули</option><option value="agent">Agent</option><option value="books">Books</option><option value="media">Media</option><option value="notes">Notes</option><option value="search">Search</option><option value="ui">UI</option><option value="desktop">Desktop</option><option value="settings">Settings</option><option value="zim">Zim</option><option value="html">HTML</option><option value="files">Файловый менеджер</option><option value="task-manager">Менеджер задач</option><option value="statistics">Статистика</option><option value="bookgen">Генератор книг</option><option value="memory">Память</option><option value="apps">Приложения</option><option value="avatar">Аватар</option><option value="education">Обучение</option><option value="collection">Коллекции</option><option value="workspace">Рабочее пространство</option><option value="article">Статьи</option><option value="ask">Вопросы AI</option><option value="list">Список</option><option value="ai">AI</option><option value="news">Новости</option><option value="search-all">Поиск по всем</option><option value="home">Главная</option><option value="genlab">GenLab</option><option value="browser">Браузер</option><option value="social">Соцсеть</option><option value="qdrant">Векторная база</option><option value="ha_panel">Умный дом</option><option value="docker">Docker</option><option value="ai_engine">AI Engine</option><option value="network">Сеть</option></select></div>
      <div><label>Точка входа (Python)</label><input id="settingsEntryPoint" placeholder="main.py"></div>
      <div><label>Frontend файл</label><input id="settingsFrontendEntry" placeholder="frontend.js"></div>
      <div><label>Git URL</label><input id="settingsGitUrl" placeholder="https://github.com/..."></div>
      <div><label>Sandbox (изоляция)</label><select id="settingsSandbox"><option value="false">Выкл</option><option value="true">Вкл</option></select></div>
      <div style="grid-column:1/-1">
        <details style="margin-top:4px">
          <summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text2)">Хуки (подписка на события)</summary>
          <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px" id="hooksContainer"></div>
        </details>
      </div>
      <div style="grid-column:1/-1">
        <details>
          <summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text2)">Разрешения</summary>
          <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px" id="permissionsContainer"></div>
        </details>
      </div>
    </div>
    <div class="modal-actions">
      <button class="btn-primary" onclick="saveExtSettings()">💾 Сохранить</button>
      <button class="btn-cancel" onclick="closeExtSettings()">Отмена</button>
    </div>
  </div>
</div>

<!-- Visual Editor Style Modal -->
<div id="visStyleModal" class="modal-overlay" style="display:none">
  <div class="modal-box" style="max-width:600px">
    <h3><i class="fas fa-palette"></i> <span id="visStyleTitle">Стили элемента</span></h3>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;font-size:12px">
      <div><label>🎨 Цвет текста</label><input type="color" id="visColor"></div>
      <div><label>🔤 Размер шрифта (px)</label><input type="range" id="visFontSize" min="8" max="72" value="16" style="width:100%"> <span id="visFontSizeVal">16</span>px</div>
      <div><label>📝 Начертание</label><select id="visFontWeight"><option value="100">Thin</option><option value="300">Light</option><option value="400" selected>Normal</option><option value="500">Medium</option><option value="600">Semibold</option><option value="700">Bold</option><option value="900">Black</option></select></div>
      <div><label>🎯 Выравнивание</label><select id="visTextAlign"><option value="left">Слева</option><option value="center">Центр</option><option value="right">Справа</option></select></div>
      <div><label>📐 Межстрочный</label><input type="number" id="visLineHeight" step="0.1" min="0.5" max="3" value="1.5" style="width:100%"></div>
      <div><label>🖌 Цвет фона</label><input type="color" id="visBgColor"></div>
      <div><label>🌫 Прозрачность фона</label><input type="range" id="visBgOpacity" min="0" max="1" step="0.05" value="1" style="width:100%"> <span id="visBgOpacityVal">1.0</span></div>
      <div><label>🔲 Border-radius (px)</label><input type="range" id="visBorderRadius" min="0" max="50" value="0" style="width:100%"> <span id="visBorderRadiusVal">0</span>px</div>
      <div style="grid-column:1/-1"><label>🌓 Тень (box-shadow)</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
          X:<input type="range" id="visShadowX" min="-20" max="20" value="0" style="width:60px"> <span id="visShadowXVal">0</span>
          Y:<input type="range" id="visShadowY" min="-20" max="20" value="4" style="width:60px"> <span id="visShadowYVal">4</span>
          Blur:<input type="range" id="visShadowBlur" min="0" max="40" value="10" style="width:60px"> <span id="visShadowBlurVal">10</span>
          Spread:<input type="range" id="visShadowSpread" min="-10" max="20" value="0" style="width:60px"> <span id="visShadowSpreadVal">0</span>
          Цвет:<input type="color" id="visShadowColor" value="#000000">
        </div>
      </div>
      <div><label>↔ Ширина</label><input id="visWidth" placeholder="auto" style="width:100%"></div>
      <div><label>↕ Высота</label><input id="visHeight" placeholder="auto" style="width:100%"></div>
      <div style="grid-column:1/-1"><label>📐 Padding</label>
        <div style="display:flex;gap:8px">Top:<input type="range" id="visPadT" min="0" max="100" value="0" style="flex:1"> <span id="visPadTVal">0</span> Bottom:<input type="range" id="visPadB" min="0" max="100" value="0" style="flex:1"> <span id="visPadBVal">0</span> Left:<input type="range" id="visPadL" min="0" max="100" value="0" style="flex:1"> <span id="visPadLVal">0</span> Right:<input type="range" id="visPadR" min="0" max="100" value="0" style="flex:1"> <span id="visPadRVal">0</span></div>
      </div>
      <div style="grid-column:1/-1"><label>📐 Margin</label>
        <div style="display:flex;gap:8px">Top:<input type="range" id="visMarT" min="-50" max="100" value="0" style="flex:1"> <span id="visMarTVal">0</span> Bottom:<input type="range" id="visMarB" min="-50" max="100" value="0" style="flex:1"> <span id="visMarBVal">0</span> Left:<input type="range" id="visMarL" min="-50" max="100" value="0" style="flex:1"> <span id="visMarLVal">0</span> Right:<input type="range" id="visMarR" min="-50" max="100" value="0" style="flex:1"> <span id="visMarRVal">0</span></div>
      </div>
      <div style="grid-column:1/-1"><label>🧱 Граница (border)</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
          W:<input type="range" id="visBorderW" min="0" max="20" value="0" style="width:50px"> <span id="visBorderWVal">0</span>
          Стиль:<select id="visBorderStyle" style="font-size:10px"><option value="solid">solid</option><option value="dashed">dashed</option><option value="dotted">dotted</option><option value="double">double</option></select>
          Цвет:<input type="color" id="visBorderColor" value="#000000">
        </div>
      </div>
      <div style="grid-column:1/-1"><label>✨ Тень текста (text-shadow)</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
          X:<input type="range" id="visTsX" min="-10" max="10" value="0" style="width:60px"> <span id="visTsXVal">0</span>
          Y:<input type="range" id="visTsY" min="-10" max="10" value="0" style="width:60px"> <span id="visTsYVal">0</span>
          Blur:<input type="range" id="visTsBlur" min="0" max="20" value="0" style="width:60px"> <span id="visTsBlurVal">0</span>
          Цвет:<input type="color" id="visTsColor" value="#000000">
        </div>
      </div>
      <div style="grid-column:1/-1"><label>🔄 Трансформация</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
          Поворот:<input type="range" id="visRotate" min="-180" max="180" value="0" style="width:80px"> <span id="visRotateVal">0</span>°
          Масштаб:<input type="range" id="visScale" min="0.1" max="3" step="0.05" value="1" style="width:80px"> <span id="visScaleVal">1.00</span>
        </div>
      </div>
            <div style="grid-column:1/-1"><label>&#x1f4d0; Display / Position</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
          Display:<select id="visDisplay" style="font-size:10px"><option value="">auto</option><option>block</option><option>inline</option><option>inline-block</option><option>flex</option><option>grid</option><option>none</option></select>
          Position:<select id="visPosition" style="font-size:10px"><option value="">auto</option><option>static</option><option>relative</option><option>absolute</option><option>fixed</option><option>sticky</option></select>
        </div>
      </div>
      <div style="grid-column:1/-1"><label>&#x2194; Coordinates</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
          Top:<input id="visTop" placeholder="auto" style="width:60px">
          Left:<input id="visLeft" placeholder="auto" style="width:60px">
          Bottom:<input id="visBottom" placeholder="auto" style="width:60px">
          Right:<input id="visRight" placeholder="auto" style="width:60px">
        </div>
      </div>
      <div><label>&#x1f300; Overflow</label><select id="visOverflow"><option value="">auto</option><option>visible</option><option>hidden</option><option>scroll</option><option>auto</option></select></div>
      <div><label>&#x1f5a5; Cursor</label><select id="visCursor"><option value="">auto</option><option>pointer</option><option>default</option><option>text</option><option>move</option><option>grab</option><option>not-allowed</option></select></div>
      <div><label>&#x1f4dd; Z-Index</label><input type="number" id="visZIndex" placeholder="auto" style="width:100%"></div>
      <div><label>&#x1f4e6; Box-Sizing</label><select id="visBoxSizing"><option value="">auto</option><option>content-box</option><option>border-box</option></select></div>
      <div style="grid-column:1/-1"><label>&#x1f5a9; Font Family</label><input id="visFontFamily" placeholder="inherit" style="width:100%"></div>
      <div><label>&#x1f517; Gap (px)</label><input id="visGap" placeholder="0" style="width:100%"></div>
      <div><label>&#x1f4dd; Letter-Spacing (px)</label><input type="range" id="visLetterSpacing" min="-5" max="20" value="0" style="width:100%"> <span id="visLetterSpacingVal">0</span></div>
      <div style="grid-column:1/-1"><label>🧩 Flex</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;font-size:10px">
          Direction:<select id="visFlexDir" style="font-size:9px"><option value="">auto</option><option>row</option><option>row-reverse</option><option>column</option><option>column-reverse</option></select>
          Wrap:<select id="visFlexWrap" style="font-size:9px"><option value="">auto</option><option>nowrap</option><option>wrap</option><option>wrap-reverse</option></select>
          Justify:<select id="visJustify" style="font-size:9px"><option value="">auto</option><option>flex-start</option><option>flex-end</option><option>center</option><option>space-between</option><option>space-around</option><option>space-evenly</option></select>
          Align:<select id="visAlignItems" style="font-size:9px"><option value="">auto</option><option>flex-start</option><option>flex-end</option><option>center</option><option>baseline</option><option>stretch</option></select>
        </div>
      </div>
      <div style="grid-column:1/-1"><label>📦 Outline</label>
        <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;font-size:10px">
          W:<input type="range" id="visOutlineW" min="0" max="20" value="0" style="width:50px"> <span id="visOutlineWVal">0</span>
          Style:<select id="visOutlineStyle" style="font-size:9px"><option value="">none</option><option>solid</option><option>dashed</option><option>dotted</option><option>double</option></select>
          Color:<input type="color" id="visOutlineColor" value="#000000">
        </div>
      </div>
      <div><label>🖼 Background-size</label><select id="visBgSize"><option value="">auto</option><option>cover</option><option>contain</option><option>100% 100%</option></select></div>
      <div><label>🔁 Background-repeat</label><select id="visBgRepeat"><option value="">repeat</option><option>no-repeat</option><option>repeat-x</option><option>repeat-y</option></select></div>
      <div><label>✏️ Text-transform</label><select id="visTextTransform"><option value="">none</option><option>capitalize</option><option>uppercase</option><option>lowercase</option></select></div>
      <div><label>📝 Text-decoration</label><select id="visTextDecor"><option value="">none</option><option>underline</option><option>overline</option><option>line-through</option></select></div>
      <div><label>👁 Visibility</label><select id="visVisibility"><option value="">visible</option><option>hidden</option><option>collapse</option></select></div>
      <div><label>&#x2194; Word-spacing</label><input type="number" id="visWordSpacing" placeholder="0" style="width:100%"></div>
      <div><label>↩ White-space</label><select id="visWhiteSpace"><option value="">normal</option><option>nowrap</option><option>pre</option><option>pre-wrap</option><option>pre-line</option></select></div>

<div style="grid-column:1/-1"><label>🏷 Атрибуты</label>
        <div id="visAttrList" style="font-size:11px;max-height:80px;overflow-y:auto;background:#f8f9fa;border-radius:4px;padding:4px">
          <span style="color:#999">нет атрибутов</span>
        </div>
      </div>
    </div>
    <div class="modal-actions">
      <button class="btn-primary" onclick="applyVisStyle()"><i class="fas fa-check"></i> Применить</button>
      <button id="visLiveBtn" title="Переключить ползунки / числовой ввод">🔀 Ползунки</button>
      <button class="btn-cancel" onclick="closeVisStyleModal()">Отмена</button>
    </div>
  </div>
</div>

<script>
let extName='{{ext_name}}';
let extDir='';
let extManifest={};
let files=[];
let openTabs=[];
let activeTab=-1;
let codeMirror=null;
let monacoInstance=null;
let useMonaco=false;
let currentFilePath='';
let terminal=null;
let terminalFitAddon=null;
let aiLastResponse='';
let extConfig={};
let extMeta={};
let allPermissions=[];
let consoleEntries=0;
let inspectorActive=false;
let visualEditorActive=false;

// Editor search
function editorSearch(){
  if(useMonaco&&monacoInstance)monacoInstance.getAction('actions.find')?.run();
  else if(codeMirror)codeMirror.execCommand('findPersistent');
}
// Editor switching
function initEditor(){
  if(useMonaco){initMonacoEditor();}else{initCodeMirrorEditor();}
}

function initCodeMirrorEditor(){
  const container=document.getElementById('editorContainer');
  if(!container)return;
  container.innerHTML='<div id="cmContainer" style="height:100%"></div>';
  codeMirror=CodeMirror(document.getElementById('cmContainer'),{
    value:'',mode:'htmlmixed',theme:'material-darker',lineNumbers:true,
    autoCloseBrackets:true,matchBrackets:true,lineWrapping:true,
    tabSize:2,indentUnit:2,styleActiveLine:true,
    extraKeys:{'Ctrl-S':function(){saveCurrentFile()},'Ctrl-F':'findPersistent','Ctrl-H':'replace','F3':'findNext','Shift-F3':'findPrev','Tab':function(cm){cm.replaceSelection('  ','end')}}
  });
}

function initMonacoEditor(){
  if(!window.monaco){
    const s=document.createElement('script');
    s.src='/static/monaco/min/vs/loader.js';
    s.onload=()=>{
      require.config({paths:{vs:'/static/monaco/min/vs'}});
      require(['vs/editor/editor.main'],()=>{setupMonacoEditor()});
    };
    document.head.appendChild(s);
  }else if(!monacoInstance){setupMonacoEditor();}
}

function setupMonacoEditor(){
  // Register Emmet provider
  if(monaco.languages.registerCompletionItemProvider){
    monaco.languages.registerCompletionItemProvider('html',{
      triggerCharacters:['.','>','/','#','[','(',')','{','}','='],
      provideCompletionItems:(model,position)=>{
        const text=model.getLineContent(position.lineNumber);
        const word=model.getWordUntilPosition(position);
        const range={startLineNumber:position.lineNumber,endLineNumber:position.lineNumber,startColumn:word.startColumn,endColumn:word.endColumn};
        const suggestions=[];
        // Emmet-like HTML completions
        const emmetSnippets=[
          {label:'div',insertText:'<div$1>$2</div>',detail:'div элемент'},
          {label:'span',insertText:'<span$1>$2</span>',detail:'span элемент'},
          {label:'a',insertText:'<a href="$1">$2</a>',detail:'ссылка'},
          {label:'img',insertText:'<img src="$1" alt="$2">',detail:'изображение'},
          {label:'ul',insertText:'<ul>\n\t<li>$1</li>\n</ul>',detail:'список'},
          {label:'li',insertText:'<li>$1</li>',detail:'элемент списка'},
          {label:'p',insertText:'<p>$1</p>',detail:'параграф'},
          {label:'h1',insertText:'<h1>$1</h1>',detail:'заголовок 1'},
          {label:'h2',insertText:'<h2>$1</h2>',detail:'заголовок 2'},
          {label:'h3',insertText:'<h3>$1</h3>',detail:'заголовок 3'},
          {label:'form',insertText:'<form action="$1" method="post">$2</form>',detail:'форма'},
          {label:'input',insertText:'<input type="$1" name="$2">',detail:'поле ввода'},
          {label:'button',insertText:'<button type="$1">$2</button>',detail:'кнопка'},
          {label:'table',insertText:'<table>\n\t<tr>\n\t\t<td>$1</td>\n\t</tr>\n</table>',detail:'таблица'},
          {label:'tr',insertText:'<tr>$1</tr>',detail:'строка таблицы'},
          {label:'td',insertText:'<td>$1</td>',detail:'ячейка таблицы'},
          {label:'section',insertText:'<section>$1</section>',detail:'секция'},
          {label:'article',insertText:'<article>$1</article>',detail:'статья'},
          {label:'header',insertText:'<header>$1</header>',detail:'шапка'},
          {label:'footer',insertText:'<footer>$1</footer>',detail:'подвал'},
          {label:'nav',insertText:'<nav>$1</nav>',detail:'навигация'},
          {label:'main',insertText:'<main>$1</main>',detail:'основной контент'},
          {label:'container',insertText:'<div class="container">$1</div>',detail:'container div'},
          {label:'wrapper',insertText:'<div class="wrapper">$1</div>',detail:'wrapper div'},
        ];
        emmetSnippets.forEach(s=>{
          suggestions.push({label:s.label,kind:monaco.languages.CompletionItemKind.Snippet,insertText:s.insertText,insertTextRules:monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,documentation:s.detail,detail:s.detail,range:range});
        });
        return{suggestions:suggestions};
      }
    });
  }

  monacoInstance=monaco.editor.create(document.getElementById('editorContainer'),{
    value:'',language:'html',theme:'vs-dark',automaticLayout:true,minimap:{enabled:true},
    fontSize:14,wordWrap:'on',scrollBeyondLastLine:false,tabSize:2,folding:true,lineNumbers:'on',
    bracketPairColorization:{enabled:true},suggestOnTriggerCharacters:true,quickSuggestions:true,
    autoClosingBrackets:'always',autoClosingQuotes:'always',tabCompletion:'on',snippetSuggestions:'inline',
    'emmet.triggerExpansionOnTab':true,'emmet.showExpandedAbbreviation':'always','emmet.showAbbreviationSuggestions':true,'emmet.includeLanguages':{'html':'html','css':'css'}
  });
  setEditorTheme();
  monacoInstance.addCommand(monaco.KeyMod.CtrlCmd|monaco.KeyCode.KeyS,()=>saveCurrentFile());
  // Emmet expand shortcut: Tab or Ctrl+E
  monacoInstance.addCommand(monaco.KeyCode.Tab,()=>{
    // Try to expand Emmet abbreviation
    const sel=monacoInstance.getSelection();
    if(sel.isEmpty()){
      const line=monacoInstance.getModel().getLineContent(sel.startLineNumber);
      const beforeCursor=line.substring(0,sel.startColumn-1);
      const abbrMatch=beforeCursor.match(/([a-z][a-z0-9]*(?:\.[\w-]+)*(?:#[\w-]+)?(?:\[\w+=\w+\])*(?:\*\d+)?)$/);
      if(abbrMatch){
        expandEmmet(abbrMatch[1],sel.startLineNumber,abbrMatch[1].length);
      }
    }
  });
}

function expandEmmet(abbr,lineNum,insertPos){
  // Simple Emmet expansion
  let html='';
  if(abbr==='div.container'||abbr==='container')html='<div class="container"></div>';
  else if(abbr==='div.wrapper'||abbr==='wrapper')html='<div class="wrapper"></div>';
  else if(abbr.startsWith('div.'))html=`<div class="${abbr.substring(4)}"></div>`;
  else if(abbr.startsWith('div#'))html=`<div id="${abbr.substring(4)}"></div>`;
  else if(abbr.startsWith('.'))html=`<div class="${abbr.substring(1)}"></div>`;
  else if(abbr.startsWith('#'))html=`<div id="${abbr.substring(1)}"></div>`;
  else return; // Not an Emmet abbreviation

  const line=monacoInstance.getModel().getLineContent(lineNum);
  const before=line.substring(0,insertPos);
  const after=line.substring(insertPos);
  const newLine=before.substring(0,insertPos-abbr.length)+html+after;
  monacoInstance.executeEdits('emmet',[{range:new monaco.Range(lineNum,1,lineNum,line.length+1),text:newLine}]);
  showToast(`Emmet: ${abbr}`);
}

function toggleEditorType(){
  if(useMonaco&&monacoInstance){if(activeTab>=0)openTabs[activeTab].content=monacoInstance.getValue();monacoInstance.dispose();monacoInstance=null;}
  else if(codeMirror){if(activeTab>=0)openTabs[activeTab].content=codeMirror.getValue();codeMirror=null;}
  useMonaco=!useMonaco;initEditor();
  if(activeTab>=0&&openTabs[activeTab]){
    setTimeout(()=>{
      if(useMonaco&&monacoInstance){monacoInstance.setValue(openTabs[activeTab].content);}
      else if(codeMirror){codeMirror.setValue(openTabs[activeTab].content);}
    },100);
  }
  showToast(useMonaco?'Monaco':'CodeMirror');
}

function initTerminal(){
  terminal=new Terminal({theme:{background:'#000',foreground:'#c9d1d9',cursor:'#58a6ff'},fontSize:13,fontFamily:'monospace',cursorBlink:true});
  terminalFitAddon=new FitAddon.FitAddon();
  terminal.loadAddon(terminalFitAddon);
  terminal.open(document.getElementById('terminal'));
  terminalFitAddon.fit();
  terminal.writeln('\x1b[1;32mInfinity Terminal\x1b[0m');
  terminal.writeln('');
  let cmdLine='';
  terminal.writeln('\x1b[33mВведите команду и нажмите Enter\x1b[0m');
  terminal.writeln('\x1b[33mДля запуска: python main.py\x1b[0m');
  terminal.writeln('');
  terminal.onKey(e=>{
    if(e.key==='\r'){
      terminal.writeln('');
      if(cmdLine.trim()){
        terminal.writeln('$ '+cmdLine);
        // Отправляем команду на сервер
        sendTerminalCmd(cmdLine);
      }
      cmdLine='';
    }
    else if(e.key==='\x7F'){if(cmdLine.length>0){cmdLine=cmdLine.slice(0,-1);terminal.write('\b \b');}}
    else if(e.key!=='\x1B'){cmdLine+=e.key;terminal.write(e.key);}
  });
}

async function sendTerminalCmd(cmd){
  try{
    const r=await fetch('/api/extensions/'+extName+'/terminal/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:cmd})});
    const d=await r.json();
    if(d.output){
      terminal.writeln(d.output);
    }else if(d.error){
      terminal.writeln('\x1b[31m'+d.error+'\x1b[0m');
    }else{
      terminal.writeln('(команда выполнена)');
    }
    terminal.writeln('');
  }catch(e){
    terminal.writeln('\x1b[31mОшибка: '+e.message+'\x1b[0m');
  }
}

// Theme toggle
function setEditorTheme(){
  var isLight=document.body.classList.contains('light');
  if(useMonaco&&monacoInstance&&window.monaco){
    monaco.editor.setTheme(isLight?'vs':'vs-dark');
  }else if(codeMirror){
    codeMirror.setOption('theme',isLight?'default':'material-darker');
  }
}
function toggleTheme(){
  var isLight=document.body.classList.toggle('light');
  localStorage.setItem('appEditTheme',isLight?'light':'dark');
  document.getElementById('themeBtn').innerHTML=isLight?'<i class="fas fa-sun"></i>':'<i class="fas fa-moon"></i>';
  setEditorTheme();
}
function applyTheme(){
  var saved=localStorage.getItem('appEditTheme');
  if(saved==='light'){
    document.body.classList.add('light');
    document.getElementById('themeBtn').innerHTML='<i class="fas fa-sun"></i>';
  }
  setEditorTheme();
}

function initDragDrop(){
  var area=document.querySelector('.editor-area');
  if(!area)return;
  area.addEventListener('dragover',function(e){e.preventDefault();area.style.outline='2px dashed #6366f1';area.style.outlineOffset='-2px';});
  area.addEventListener('dragleave',function(){area.style.outline='';area.style.outlineOffset='';});
  area.addEventListener('drop',function(e){
    e.preventDefault();area.style.outline='';area.style.outlineOffset='';
    if(!e.dataTransfer||!e.dataTransfer.files)return;
    for(var i=0;i<e.dataTransfer.files.length;i++){
      (function(f){
        var r=new FileReader();
        r.onload=function(ev){
          openTabs.push({name:f.name,content:ev.target.result,unsaved:false});
          renderEditorTabs();switchTab(openTabs.length-1);
          showToast('📄 Открыт: '+f.name);
        };
        r.readAsText(f);
      })(e.dataTransfer.files[i]);
    }
  });
}
function addKeyBindings(){
  document.addEventListener('keydown',function(e){
    if(e.ctrlKey&&e.key==='w'){e.preventDefault();if(activeTab>=0)closeTab(activeTab);}
  });
}
function isColorStr(s){
  if(!s||!s.trim())return false;
  return /^#[0-9a-f]{3,8}$/i.test(s.trim())||/^rgb\s*\(/.test(s.trim())||/^rgba\s*\(/.test(s.trim())||/^hsl\s*\(/.test(s.trim());
}
function initColorPicker(){
  // Monaco click on color
  if(useMonaco&&monacoInstance){
    document.querySelector('.editor-area')?.addEventListener('mouseup',function(){
      var pos=monacoInstance.getPosition();
      if(!pos)return;
      var range=monacoInstance.getModel().getWordAtPosition(pos);
      if(!range)return;
      var word=monacoInstance.getModel().getValueInRange(range);
      if(isColorStr(word)){
        var p=document.getElementById('editorContainer').getBoundingClientRect();
        showColorPopup(word,function(newColor){
          monacoInstance.executeEdits('colorpicker',[{range:new monaco.Range(pos.lineNumber,range.startColumn,pos.lineNumber,range.endColumn),text:newColor}]);
        });
      }
    });
  }
  // CodeMirror click on color
  if(codeMirror){
    codeMirror.on('cursorActivity',function(){
      // Debounce: only when selection changes
      var cur=codeMirror.getCursor();
      if(!cur)return;
      var tok=codeMirror.getTokenAt(cur);
      if(tok&&isColorStr(tok.string)){
        codeMirror._colorTok=tok;
      }else{
        codeMirror._colorTok=null;
      }
    });
    codeMirror.on('dblclick',function(){
      var tok=codeMirror._colorTok;
      if(tok&&isColorStr(tok.string)){
        showColorPopup(tok.string,function(newColor){
          var from={line:codeMirror.getCursor().line,ch:tok.start};
          var to={line:codeMirror.getCursor().line,ch:tok.end};
          codeMirror.replaceRange(newColor,from,to);
        });
      }
    });
  }
}
var _colorPopup=null;
function showColorPopup(currentColor,callback){
  if(_colorPopup){_colorPopup.remove();_colorPopup=null;}
  var popup=document.createElement('div');
  popup.style.cssText='position:fixed;z-index:9999;background:var(--bg2, #1c242f);border:1px solid var(--glass-border, rgba(255,255,255,0.1));border-radius:10px;padding:10px;box-shadow:0 8px 32px rgba(0,0,0,0.4);display:flex;flex-direction:column;gap:6px;align-items:center';
  var input=document.createElement('input');
  input.type='color';
  input.value=normalizeColor(currentColor);
  input.style.cssText='width:120px;height:30px;border:none;border-radius:6px;cursor:pointer;background:transparent';
  var applyBtn=document.createElement('button');
  applyBtn.textContent='Применить';
  applyBtn.style.cssText='padding:4px 12px;border-radius:6px;border:none;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;cursor:pointer;font-size:11px';
  applyBtn.onclick=function(){
    callback(input.value);
    popup.remove();_colorPopup=null;
  };
  popup.appendChild(input);
  popup.appendChild(applyBtn);
  // Position near cursor
  var sel=window.getSelection();
  if(sel&&sel.rangeCount>0){
    var r=sel.getRangeAt(0);
    var rect=r.getBoundingClientRect();
    popup.style.left=Math.min(rect.right+10,window.innerWidth-160)+'px';
    popup.style.top=Math.min(rect.top-10,window.innerHeight-120)+'px';
  }else{
    popup.style.left='50%';popup.style.top='50%';
  }
  document.body.appendChild(popup);
  _colorPopup=popup;
  // Close on click outside
  setTimeout(function(){
    document.addEventListener('click',function cl(e){if(!popup.contains(e.target)){popup.remove();_colorPopup=null;document.removeEventListener('click',cl);}},{once:true});
  },100);
}
function normalizeColor(str){
  var s=(str||'').trim().toLowerCase();
  if(/^#[0-9a-f]{3,8}$/.test(s)){
    if(s.length===4)return'#'+s[1]+s[1]+s[2]+s[2]+s[3]+s[3];
    return s;
  }
  if(s.startsWith('rgb')){
    var m=s.match(/(\d+)/g);
    if(m)return'#'+('0'+parseInt(m[0]).toString(16)).slice(-2)+('0'+parseInt(m[1]).toString(16)).slice(-2)+('0'+parseInt(m[2]).toString(16)).slice(-2);
  }
  return'#6366f1';
}
// Init
window.addEventListener('DOMContentLoaded',function(){initEditor();initTerminal();loadExtInfo();loadFiles();initConsoleListener();initResizers();updateResizers();applyTheme();initDragDrop();addKeyBindings();initColorPicker();});

async function loadExtInfo(){
  try{
    const [mr, pr] = await Promise.all([
      fetch('/api/extensions/'+extName+'/metadata'),
      fetch('/api/extensions/permissions/list-all')
    ]);
    const md=await mr.json(); extMeta=md.metadata||{};
    const pd=await pr.json(); allPermissions=pd.permissions||[];
    extConfig=extMeta.config||{};
    document.getElementById('extTitle').textContent=extMeta.name||extName;
  }catch(e){console.error(e)}
}

async function reloadExtension(){
  try{
    const el=document.getElementById('reloadBtn');
    el.className='status-pill'; el.innerHTML='<i class="fas fa-spinner fa-pulse"></i> Перезагрузка...';
    const r=await fetch('/api/extensions/'+extName+'/reload',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      el.className='status-pill'; el.innerHTML='<i class="fas fa-check-circle" style="color:var(--green)"></i> OK';
      showToast('✓ Расширение перезагружено');
      setTimeout(()=>{el.className='status-pill'; el.innerHTML='<i class="fas fa-sync-alt"></i> Перезагрузить';},2000);
    }else{
      el.className='status-pill'; el.innerHTML='<i class="fas fa-exclamation-circle" style="color:var(--red)"></i> Ошибка';
      showToast('Ошибка: '+(d.error||''));
    }
  }catch(e){showToast('Ошибка: '+e.message)}
}

// Состояние раскрытых папок
let expandedFolders=new Set();
let fileTree=[]; // Дерево файлов

function renderFileTree(){
  const tree=document.getElementById('fileTree');
  tree.innerHTML=renderTreeItems(fileTree,0);
}

// Строим дерево из плоского списка файлов
function buildTree(flatFiles){
  const root=[];
  const folderMap={};

  // Сначала создаём все папки
  flatFiles.forEach(f=>{
    if(f.type==='folder'){
      folderMap[f.path]={...f,children:[]};
    }
  });

  // Распределяем файлы и папки
  flatFiles.forEach(f=>{
    if(f.type==='folder'){
      const folder=folderMap[f.path];
      const parentPath=f.path.includes('/')?f.path.substring(0,f.path.lastIndexOf('/')):'';
      if(parentPath&&folderMap[parentPath]){
        folderMap[parentPath].children.push(folder);
      }else{
        root.push(folder);
      }
    }else{
      const parentPath=f.path.includes('/')?f.path.substring(0,f.path.lastIndexOf('/')):'';
      if(parentPath&&folderMap[parentPath]){
        folderMap[parentPath].children.push(f);
      }else{
        root.push(f);
      }
    }
  });

  return root;
}

function renderTreeItems(items,indent){
  if(!items||!items.length)return'';
  let html='';
  items.forEach(f=>{
    const isFolder=f.type==='folder';
    const isExpanded=expandedFolders.has(f.path);
    const icon=isFolder?(isExpanded?'fa-folder-open':'fa-folder'):
      f.name.endsWith('.html')?'fa-file-code':f.name.endsWith('.js')?'fa-file-alt':
      f.name.endsWith('.css')?'fa-palette':f.name.endsWith('.json')?'fa-cog':
      f.name.endsWith('.py')?'fa-python':f.name.endsWith('.md')?'fa-file-alt':'fa-file';
    const active=f.path===currentFilePath?'active':'';
    const paddingLeft=indent*16;

    if(isFolder){
      html+=`<div class="file-item ${active}" onclick="toggleFolder('${f.path.replace(/'/g,"\\'")}')" style="padding-left:${paddingLeft}px;cursor:pointer">
        <i class="fas ${icon}"></i><span>${f.name}</span>
        <span class="del" onclick="event.stopPropagation();deleteFile('${f.path.replace(/'/g,"\\'")}')"><i class="fas fa-times"></i></span>
      </div>`;
      if(isExpanded&&f.children&&f.children.length){
        html+=renderTreeItems(f.children,indent+1);
      }
    }else{
      html+=`<div class="file-item ${active}" onclick="openFile('${f.path.replace(/'/g,"\\'")}')" style="padding-left:${paddingLeft}px">
        <i class="fas ${icon}"></i><span>${f.name}</span>
        <span class="del" onclick="event.stopPropagation();deleteFile('${f.path.replace(/'/g,"\\'")}')"><i class="fas fa-times"></i></span>
      </div>`;
    }
  });
  return html;
}

function toggleFolder(path){
  if(expandedFolders.has(path))expandedFolders.delete(path);
  else expandedFolders.add(path);
  renderFileTree();
}

async function openFile(path){
  if(activeTab>=0&&openTabs[activeTab]){
    if(useMonaco&&monacoInstance)openTabs[activeTab].content=monacoInstance.getValue();
    else if(codeMirror)openTabs[activeTab].content=codeMirror.getValue();
  }
  currentFilePath=path;renderFileTree();
  const idx=openTabs.findIndex(t=>t.path===path);
  if(idx>=0){switchTab(idx);return}
  try{
    const r=await fetch('/api/extensions/'+extName+'/files/read?path='+encodeURIComponent(path));
    const d=await r.json();
    if(d.error){showToast('Ошибка: '+d.error);return}
    const name=path.split('/').pop();
    const content=d.content!==undefined?d.content:'';
    openTabs.push({path,content:content,name});
    if(!codeMirror&&!useMonaco){
      let retries=0;
      while(!codeMirror&&!useMonaco&&retries<20){
        await new Promise(r=>setTimeout(r,50));
        retries++;
      }
    }
    switchTab(openTabs.length-1);
  }catch(e){console.error('openFile error:',e);showToast('Ошибка: '+e.message)}
}

function switchTab(idx){
  const oldIdx=activeTab;
  activeTab=idx;const tab=openTabs[idx];if(!tab)return;
  // Сохраняем содержимое старой вкладки
  if(oldIdx>=0&&oldIdx<openTabs.length&&oldIdx!==idx){
    if(useMonaco&&monacoInstance){openTabs[oldIdx].content=monacoInstance.getValue();}
    else if(codeMirror){openTabs[oldIdx].content=codeMirror.getValue();}
  }
  if(useMonaco&&monacoInstance){
    monacoInstance.setValue(tab.content||'');
    const ext=tab.name.split('.').pop().toLowerCase();
    const mm={js:'javascript',py:'python',json:'json',css:'css',html:'html',md:'markdown'};
    monaco.editor.setModelLanguage(monacoInstance.getModel(),mm[ext]||'html');
  }else if(codeMirror){
    codeMirror.setValue(tab.content||'');
    const ext=tab.name.split('.').pop().toLowerCase();
    const mm={js:'javascript',py:'python',json:'application/json',css:'css',html:'htmlmixed',txt:'text/plain'};
    codeMirror.setOption('mode',mm[ext]||'htmlmixed');
    setTimeout(()=>{codeMirror.refresh();},50);
  }
  renderEditorTabs();renderFileTree();
}

function renderEditorTabs(){
  document.getElementById('editorTabs').innerHTML=openTabs.map((t,i)=>`
    <div class="editor-tab ${i===activeTab?'active':''}" onclick="switchTab(${i})" onauxclick="if(event.button===1){event.preventDefault();closeTab(${i})}">
      <i class="fas fa-file"></i>${t.name}<span class="close" onclick="event.stopPropagation();closeTab(${i})">✕</span>
    </div>`).join('');
}

function closeTab(idx){
  openTabs.splice(idx,1);
  if(activeTab>=openTabs.length)activeTab=openTabs.length-1;
  if(activeTab>=0)switchTab(activeTab);
  else if(codeMirror)codeMirror.setValue('');
  renderEditorTabs();
}

async function saveCurrentFile(){
  if(activeTab<0||!currentFilePath)return;
  let content='';
  if(useMonaco&&monacoInstance)content=monacoInstance.getValue();
  else if(codeMirror)content=codeMirror.getValue();
  else content=openTabs[activeTab].content;
  openTabs[activeTab].content=content;
  try{
    const r=await fetch('/api/extensions/'+extName+'/files/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:currentFilePath,content})});
    const d=await r.json();
    if(d.ok){
      showToast('✓ Сохранено');
      await fetch('/api/extensions/'+extName+'/reload',{method:'POST'});
    }else{
      showToast('Ошибка: '+(d.error||'неизвестная ошибка'),'error');
    }
  }catch(e){showToast('Ошибка: '+e.message,'error')}
}

function togglePreview(){const a=document.getElementById('previewArea');a.classList.toggle('show');if(a.classList.contains('show'))refreshPreview();updateResizers();}
function refreshPreview(){
  if(activeTab<0)return;
  const tab=openTabs[activeTab];
  const ext=tab.name.split('.').pop().toLowerCase();
   var sc='<script data-vis="1">', ec='<'+'/script>';
  var extraScripts=sc+getConsoleCaptureScript()+ec;
  if(inspectorActive)extraScripts+=sc+getInspectorScript()+ec;
  if(visualEditorActive)extraScripts+=sc+getVisualEditorScript()+ec;
  if(ext==='html'||ext==='htm'){
    var content=tab.content;
    // Inject extra scripts right after <head> so they run before all other scripts
    if(content.indexOf('<head>')>=0){
      content=content.replace('<head>','<head>'+extraScripts);
    }else if(content.indexOf('<html')>=0){
      content=content.replace('>','>'+extraScripts);
    }else{
      content=extraScripts+content;
    }
    document.getElementById('previewFrame').srcdoc=content;
  }else if(ext==='css'){
    document.getElementById('previewFrame').srcdoc='<html><head><style>'+tab.content+'</style></head><body>'+extraScripts+'<h1>CSS превью</h1><p>Текст для проверки стилей</p></body></html>';
  }else if(ext==='js'){
    document.getElementById('previewFrame').srcdoc='<html><body>'+extraScripts+'<script>'+tab.content+'<\/script></body></html>';
  }else if(ext==='py'){
    document.getElementById('previewFrame').srcdoc='<html><body style="padding:20px;font-family:monospace">'+extraScripts+'<h3>Python файл</h3><p>Для запуска используйте терминал или кнопку "Сервер"</p><pre style="background:#f5f5f5;padding:12px;border-radius:8px;overflow:auto;max-height:400px">'+esc(tab.content)+'</pre></body></html>';
  }
}
var visSelectedElPath='';
var visMsgId=0;

function showStyleModal(action,currentStyles){
  console.log('vis:showStyleModal',action,currentStyles);
  var m=document.getElementById('visStyleModal');
  if(!m){console.error('vis:modal element not found');return;}
  m.classList.add('side-panel');
  m.style.display='flex';
  function gv(id){var e=document.getElementById(id);if(!e)console.error('vis:missing id',id);return e;}
  function sv(id,val){var e=gv(id);if(e)e.value=val;}
  function tv(id,val){var e=gv(id);if(e)e.textContent=val;}
  tv('visStyleTitle','Стили: '+action);
  sv('visColor',currentStyles.color||'#000000');
  var fs=parseInt(currentStyles.fontSize)||16;
  sv('visFontSize',fs);tv('visFontSizeVal',fs);
  sv('visFontWeight',currentStyles.fontWeight||'400');
  sv('visTextAlign',currentStyles.textAlign||'left');
  sv('visLineHeight',currentStyles.lineHeight||'1.5');
  sv('visBgColor',currentStyles.bgColor||'#ffffff');
  var op=parseFloat(currentStyles.bgOpacity)||1;
  sv('visBgOpacity',op);tv('visBgOpacityVal',op.toFixed(1));
  var br=parseInt(currentStyles.borderRadius)||0;
  sv('visBorderRadius',br);tv('visBorderRadiusVal',br);
  sv('visShadowX',parseInt(currentStyles.shadowX)||0);
  sv('visShadowY',parseInt(currentStyles.shadowY)||4);
  sv('visShadowBlur',parseInt(currentStyles.shadowBlur)||10);
  sv('visShadowSpread',parseInt(currentStyles.shadowSpread)||0);
  sv('visShadowColor',currentStyles.shadowColor||'#000000');
  var sxEl=gv('visShadowX'),syEl=gv('visShadowY'),sblEl=gv('visShadowBlur'),sspEl=gv('visShadowSpread');
  tv('visShadowXVal',sxEl?sxEl.value:'0');tv('visShadowYVal',syEl?syEl.value:'0');
  tv('visShadowBlurVal',sblEl?sblEl.value:'0');tv('visShadowSpreadVal',sspEl?sspEl.value:'0');
  sv('visWidth',currentStyles.width||'');
  sv('visHeight',currentStyles.height||'');
  sv('visPadT',parseInt(currentStyles.padT)||0);
  sv('visPadB',parseInt(currentStyles.padB)||0);
  sv('visPadL',parseInt(currentStyles.padL)||0);
  sv('visPadR',parseInt(currentStyles.padR)||0);
  tv('visPadTVal',currentStyles.padT||0);
  tv('visPadBVal',currentStyles.padB||0);
  tv('visPadLVal',currentStyles.padL||0);
  tv('visPadRVal',currentStyles.padR||0);
  sv('visMarT',parseInt(currentStyles.marT)||0);
  sv('visMarB',parseInt(currentStyles.marB)||0);
  sv('visMarL',parseInt(currentStyles.marL)||0);
  sv('visMarR',parseInt(currentStyles.marR)||0);
  tv('visMarTVal',currentStyles.marT||0);
  tv('visMarBVal',currentStyles.marB||0);
  tv('visMarLVal',currentStyles.marL||0);
  tv('visMarRVal',currentStyles.marR||0);
  sv('visBorderW',parseInt(currentStyles.borderWidth)||0);
  tv('visBorderWVal',parseInt(currentStyles.borderWidth)||0);
  sv('visBorderStyle',currentStyles.borderStyle||'solid');
  sv('visBorderColor',currentStyles.borderColor||'#000000');
  sv('visTsX',parseInt(currentStyles.textShadowX)||0);
  tv('visTsXVal',parseInt(currentStyles.textShadowX)||0);
  sv('visTsY',parseInt(currentStyles.textShadowY)||0);
  tv('visTsYVal',parseInt(currentStyles.textShadowY)||0);
  sv('visTsBlur',parseInt(currentStyles.textShadowBlur)||0);
  tv('visTsBlurVal',parseInt(currentStyles.textShadowBlur)||0);
  sv('visTsColor',currentStyles.textShadowColor||'#000000');
  sv('visRotate',parseInt(currentStyles.transformRotate)||0);
  tv('visRotateVal',parseInt(currentStyles.transformRotate)||0);
  sv('visScale',parseFloat(currentStyles.transformScale)||1);
  tv('visScaleVal',(parseFloat(currentStyles.transformScale)||1).toFixed(2));
  sv('visDisplay',currentStyles.display||'');
  sv('visPosition',currentStyles.position||'');
  sv('visTop',currentStyles.top||'');sv('visLeft',currentStyles.left||'');
  sv('visRight',currentStyles.right||'');sv('visBottom',currentStyles.bottom||'');
  sv('visOverflow',currentStyles.overflow||'');
  sv('visFontFamily',currentStyles.fontFamily||'');
  sv('visCursor',currentStyles.cursor||'');
  sv('visZIndex',currentStyles.zIndex||'');
  sv('visBoxSizing',currentStyles.boxSizing||'');
  sv('visGap',currentStyles.gap||'');
  var ls=parseInt(currentStyles.letterSpacing)||0;
  sv('visLetterSpacing',ls);tv('visLetterSpacingVal',ls);
  sv('visFlexDir',currentStyles.flexDirection||'');
  sv('visFlexWrap',currentStyles.flexWrap||'');
  sv('visJustify',currentStyles.justifyContent||'');
  sv('visAlignItems',currentStyles.alignItems||'');
  var ow=parseInt(currentStyles.outlineWidth)||0;
  sv('visOutlineW',ow);tv('visOutlineWVal',ow);
  sv('visOutlineStyle',currentStyles.outlineStyle||'');
  sv('visOutlineColor',currentStyles.outlineColor||'#000000');
  sv('visBgSize',currentStyles.backgroundSize||'');
  sv('visBgRepeat',currentStyles.backgroundRepeat||'');
  sv('visTextTransform',currentStyles.textTransform||'');
  sv('visTextDecor',currentStyles.textDecoration||'');
  sv('visVisibility',currentStyles.visibility||'');
  sv('visWordSpacing',currentStyles.wordSpacing||'');
  sv('visWhiteSpace',currentStyles.whiteSpace||'');
  var attrContainer=gv('visAttrList');
  if(attrContainer&&currentStyles.attrs){
    var html='';var keys=Object.keys(currentStyles.attrs);
    if(keys.length){keys.forEach(function(k){html+='<span style="color:#6366f1">'+k+'</span>=<span style="color:#10b981">"'+currentStyles.attrs[k]+'"</span> ';});}
    else{html='<span style="color:#999">нет атрибутов</span>';}
    attrContainer.innerHTML=html;
  }
  m.dataset.transformOrig=currentStyles.transformOrig||'';
  // Live preview on every input change
  var allInputs=m.querySelectorAll('input,select');
  allInputs.forEach(function(el){
    el.oninput=function(){sendVisLivePreview();};
    if(el.type==='range')el.oninput=function(){sendVisLivePreview();updateSliderVal(el);};
  });
  // Global toggle for slider/number mode
  var toggleBtn=document.getElementById('visLiveBtn');
  if(toggleBtn)toggleBtn.onclick=function(){toggleVisInputMode();};
}
function updateSliderVal(el){
  var valEl=document.getElementById(el.id+'Val');
  if(valEl)valEl.textContent=el.id.indexOf('Scale')>=0?parseFloat(el.value).toFixed(2):el.value;
}
function toggleVisInputMode(){
  var m=document.getElementById('visStyleModal');
  var isNum=m.classList.toggle('vis-num-mode');
  var btn=document.getElementById('visLiveBtn');
  if(btn)btn.textContent=isNum?'🔢 Числовой':'🔀 Ползунки';
  var ranges=m.querySelectorAll('input[type=range]');
  ranges.forEach(function(r){
    var existing=r.nextElementSibling;
    if(existing&&existing.classList.contains('vis-num-input'))existing.remove();
    if(isNum){
      var inp=document.createElement('input');
      inp.type='number';
      inp.className='vis-num-input';
      inp.value=r.value;
      inp.step=r.step||'1';
      inp.min=r.min;
      inp.max=r.max;
      inp.style.display='inline-block';
      inp.oninput=function(){r.value=this.value;sendVisLivePreview();};
      r.parentNode.insertBefore(inp,r.nextSibling);
    }
  });
}
function sendVisLivePreview(){
  var data={
    color:document.getElementById('visColor').value,
    fontSize:document.getElementById('visFontSize').value,
    fontWeight:document.getElementById('visFontWeight').value,
    textAlign:document.getElementById('visTextAlign').value,
    lineHeight:document.getElementById('visLineHeight').value,
    bgColor:document.getElementById('visBgColor').value,
    bgOpacity:document.getElementById('visBgOpacity').value,
    borderRadius:document.getElementById('visBorderRadius').value,
    shadowX:document.getElementById('visShadowX').value,
    shadowY:document.getElementById('visShadowY').value,
    shadowBlur:document.getElementById('visShadowBlur').value,
    shadowSpread:document.getElementById('visShadowSpread').value,
    shadowColor:document.getElementById('visShadowColor').value,
    width:document.getElementById('visWidth').value,
    height:document.getElementById('visHeight').value,
    padT:document.getElementById('visPadT').value,
    padB:document.getElementById('visPadB').value,
    padL:document.getElementById('visPadL').value,
    padR:document.getElementById('visPadR').value,
    marT:document.getElementById('visMarT').value,
    marB:document.getElementById('visMarB').value,
    marL:document.getElementById('visMarL').value,
    marR:document.getElementById('visMarR').value,
    borderWidth:document.getElementById('visBorderW').value,
    borderStyle:document.getElementById('visBorderStyle').value,
    borderColor:document.getElementById('visBorderColor').value,
    textShadowX:document.getElementById('visTsX').value,
    textShadowY:document.getElementById('visTsY').value,
    textShadowBlur:document.getElementById('visTsBlur').value,
    textShadowColor:document.getElementById('visTsColor').value,
    transformRotate:document.getElementById('visRotate').value,
    transformScale:document.getElementById('visScale').value,
    transformOrig:document.getElementById('visStyleModal').dataset.transformOrig||'',
    display:document.getElementById('visDisplay').value,
    position:document.getElementById('visPosition').value,
    top:document.getElementById('visTop').value,
    left:document.getElementById('visLeft').value,
    right:document.getElementById('visRight').value,
    bottom:document.getElementById('visBottom').value,
    overflow:document.getElementById('visOverflow').value,
    fontFamily:document.getElementById('visFontFamily').value,
    cursor:document.getElementById('visCursor').value,
    zIndex:document.getElementById('visZIndex').value,
    boxSizing:document.getElementById('visBoxSizing').value,
    gap:document.getElementById('visGap').value,
    letterSpacing:document.getElementById('visLetterSpacing').value,
    flexDirection:document.getElementById('visFlexDir').value,
    flexWrap:document.getElementById('visFlexWrap').value,
    justifyContent:document.getElementById('visJustify').value,
    alignItems:document.getElementById('visAlignItems').value,
    outlineWidth:document.getElementById('visOutlineW').value,
    outlineStyle:document.getElementById('visOutlineStyle').value,
    outlineColor:document.getElementById('visOutlineColor').value,
    backgroundSize:document.getElementById('visBgSize').value,
    backgroundRepeat:document.getElementById('visBgRepeat').value,
    textTransform:document.getElementById('visTextTransform').value,
    textDecoration:document.getElementById('visTextDecor').value,
    visibility:document.getElementById('visVisibility').value,
    wordSpacing:document.getElementById('visWordSpacing').value,
    whiteSpace:document.getElementById('visWhiteSpace').value
  };
  var frame=document.getElementById('previewFrame');
  if(frame&&frame.contentWindow){
    frame.contentWindow.postMessage({source:'visEditorResult',status:'preview',data:data},'*');
  }
}

function closeVisStyleModal(){
  var m=document.getElementById('visStyleModal');
  m.classList.remove('side-panel','vis-num-mode');
  m.style.display='none';
  m.querySelectorAll('.vis-num-input').forEach(function(e){e.remove();});
}
// Escape and backdrop click close the side panel
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'){
    var vm=document.getElementById('visStyleModal');
    if(vm.style.display==='flex')closeVisStyleModal();
  }
});
document.getElementById('visStyleModal').addEventListener('click',function(e){
  if(e.target===this)closeVisStyleModal();
});

function applyVisStyle(){
  var data={
    color:document.getElementById('visColor').value,
    fontSize:document.getElementById('visFontSize').value,
    fontWeight:document.getElementById('visFontWeight').value,
    textAlign:document.getElementById('visTextAlign').value,
    lineHeight:document.getElementById('visLineHeight').value,
    bgColor:document.getElementById('visBgColor').value,
    bgOpacity:document.getElementById('visBgOpacity').value,
    borderRadius:document.getElementById('visBorderRadius').value,
    shadowX:document.getElementById('visShadowX').value,
    shadowY:document.getElementById('visShadowY').value,
    shadowBlur:document.getElementById('visShadowBlur').value,
    shadowSpread:document.getElementById('visShadowSpread').value,
    shadowColor:document.getElementById('visShadowColor').value,
    width:document.getElementById('visWidth').value,
    height:document.getElementById('visHeight').value,
    padT:document.getElementById('visPadT').value,
    padB:document.getElementById('visPadB').value,
    padL:document.getElementById('visPadL').value,
    padR:document.getElementById('visPadR').value,
    marT:document.getElementById('visMarT').value,
    marB:document.getElementById('visMarB').value,
    marL:document.getElementById('visMarL').value,
    marR:document.getElementById('visMarR').value,
    borderWidth:document.getElementById('visBorderW').value,
    borderStyle:document.getElementById('visBorderStyle').value,
    borderColor:document.getElementById('visBorderColor').value,
    textShadowX:document.getElementById('visTsX').value,
    textShadowY:document.getElementById('visTsY').value,
    textShadowBlur:document.getElementById('visTsBlur').value,
    textShadowColor:document.getElementById('visTsColor').value,
    transformRotate:document.getElementById('visRotate').value,
    transformScale:document.getElementById('visScale').value,
    transformOrig:document.getElementById('visStyleModal').dataset.transformOrig||'',
    display:document.getElementById('visDisplay').value,
    position:document.getElementById('visPosition').value,
    top:document.getElementById('visTop').value,
    left:document.getElementById('visLeft').value,
    right:document.getElementById('visRight').value,
    bottom:document.getElementById('visBottom').value,
    overflow:document.getElementById('visOverflow').value,
    fontFamily:document.getElementById('visFontFamily').value,
    cursor:document.getElementById('visCursor').value,
    zIndex:document.getElementById('visZIndex').value,
    boxSizing:document.getElementById('visBoxSizing').value,
    gap:document.getElementById('visGap').value,
    letterSpacing:document.getElementById('visLetterSpacing').value,
    flexDirection:document.getElementById('visFlexDir').value,
    flexWrap:document.getElementById('visFlexWrap').value,
    justifyContent:document.getElementById('visJustify').value,
    alignItems:document.getElementById('visAlignItems').value,
    outlineWidth:document.getElementById('visOutlineW').value,
    outlineStyle:document.getElementById('visOutlineStyle').value,
    outlineColor:document.getElementById('visOutlineColor').value,
    backgroundSize:document.getElementById('visBgSize').value,
    backgroundRepeat:document.getElementById('visBgRepeat').value,
    textTransform:document.getElementById('visTextTransform').value,
    textDecoration:document.getElementById('visTextDecor').value,
    visibility:document.getElementById('visVisibility').value,
    wordSpacing:document.getElementById('visWordSpacing').value,
    whiteSpace:document.getElementById('visWhiteSpace').value
  };
  closeVisStyleModal();
  var frame=document.getElementById('previewFrame');
  if(frame&&frame.contentWindow){
    frame.contentWindow.postMessage({source:'visEditorResult',status:'ok',data:data},'*');
  }
}

function toggleInspector(){
  inspectorActive=!inspectorActive;
  var btn=document.getElementById('inspectBtn');
  btn.classList.toggle('toolbar-btn-primary',inspectorActive);
  document.getElementById('inspectInfo').style.display=inspectorActive?'block':'none';
  if(inspectorActive)showToast('🎯 Инспектор включён — наведите на элемент');
  refreshPreview();
}
function getInspectorScript(){
  return '(function(){'+
    'var active=false;var tooltip=null;var infoPanel=null;'+
    'function createTooltip(){tooltip=document.createElement("div");tooltip.style.cssText="position:fixed;pointer-events:none;z-index:99999;background:rgba(15,20,25,0.92);color:#e4e8ef;padding:6px 10px;border-radius:8px;font-size:11px;font-family:JetBrains Mono,monospace;line-height:1.5;border:1px solid rgba(255,255,255,0.1);backdrop-filter:blur(8px);max-width:400px;white-space:pre-wrap";document.body.appendChild(tooltip);}'+
    'function createInfoPanel(){infoPanel=document.createElement("div");infoPanel.id="__inspector_info";infoPanel.style.cssText="position:fixed;bottom:12px;left:12px;right:12px;z-index:99999;background:rgba(15,20,25,0.92);color:#e4e8ef;padding:10px 14px;border-radius:12px;font-size:11px;font-family:JetBrains Mono,monospace;line-height:1.6;border:1px solid rgba(255,255,255,0.1);backdrop-filter:blur(12px);max-height:200px;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,0.4)";document.body.appendChild(infoPanel);}'+
    'function getStyles(el){var s=getComputedStyle(el);return{'+
      '"tag":el.tagName.toLowerCase(),'+
      '"id":el.id||"",'+
      '"classes":Array.from(el.classList).join("."),'+
      '"attrs":Array.from(el.attributes).map(function(a){return a.name+(a.value?"=\\""+a.value+"\\"":"")}).join(" "),'+
      '"size":el.offsetWidth+"x"+el.offsetHeight,'+
      '"color":s.color,'+
      '"bg":s.backgroundImage!=="none"?s.backgroundImage:s.backgroundColor,'+
      '"font":s.fontSize+" "+s.fontFamily,'+
      '"fw":s.fontWeight,'+
      '"display":s.display,'+
      '"position":s.position,'+
      '"padding":s.paddingTop+" "+s.paddingRight+" "+s.paddingBottom+" "+s.paddingLeft,'+
      '"margin":s.marginTop+" "+s.marginRight+" "+s.marginBottom+" "+s.marginLeft,'+
      '"border":s.borderWidth+" "+s.borderStyle+" "+s.borderColor,'+
      '"br":s.borderRadius,'+
      '"flex":s.flex||"-",'+
      '"grid":s.gridTemplateColumns||"-",'+
      '"opacity":s.opacity,'+
      '"zIndex":s.zIndex,'+
      '"boxShadow":s.boxShadow,'+
      '"text":(el.textContent||"").trim().substring(0,80)'+
    '};}'+
    'document.addEventListener("mouseover",function(e){'+
      'var el=e.target;if(el===tooltip||el.parentNode===tooltip||el===infoPanel||el.parentNode===infoPanel)return;'+
      'if(!tooltip)createTooltip();'+
      'var s=getStyles(el);'+
      'tooltip.style.display="block";'+
      'tooltip.innerHTML="<b style=\\"color:#6366f1\\">&lt;"+s.tag+"&gt;</b>"+(s.id?" <span style=\\"color:#f97316\\">#"+s.id+"</span>":"")+(s.classes?" <span style=\\"color:#10b981\\">."+s.classes+"</span>":"")+"<br><span style=\\"color:#a0aec0\\">"+s.size+"</span>"+(s.text?"<br><span style=\\"color:#6b7280;font-size:10px\\">"+esc_inner(s.text)+"</span>":"");'+
      'var r=el.getBoundingClientRect();'+
      'tooltip.style.left=Math.min(r.left+8,window.innerWidth-tooltip.offsetWidth-8)+"px";'+
      'tooltip.style.top=Math.max(0,r.top-tooltip.offsetHeight-6)+"px";'+
      'el.style.outline="2px solid #6366f1";el.style.outlineOffset="-2px";'+
    '},true);'+
    'document.addEventListener("mouseout",function(e){'+
      'var el=e.target;if(el===tooltip||el.parentNode===tooltip||el===infoPanel||el.parentNode===infoPanel)return;'+
      'if(tooltip)tooltip.style.display="none";'+
      'el.style.outline="";el.style.outlineOffset="";'+
    '},true);'+
    'document.addEventListener("click",function(e){'+
      'var el=e.target;if(el===tooltip||el.parentNode===tooltip||el===infoPanel||el.parentNode===infoPanel)return;'+
      'e.preventDefault();e.stopPropagation();'+
      'if(!infoPanel)createInfoPanel();'+
      'var s=getStyles(el);'+
      'var html="<div style=\\"display:flex;gap:12px;flex-wrap:wrap\\">";'+
      'html+="<div><b style=\\"color:#6366f1\\"># "+s.tag+"</b>";'+
      'if(s.id)html+=" <span style=\\"color:#f97316\\">#"+s.id+"</span>";'+
      'if(s.classes)html+=" <span style=\\"color:#10b981\\">."+s.classes+"</span>";'+
      'html+="</div>";'+
      'html+="<div style=\\"color:#a0aec0\\">"+s.size+"</div>";'+
      'html+="</div>";'+
      'html+="<div style=\\"display:grid;grid-template-columns:1fr 1fr;gap:2px 16px;margin-top:6px\\">";'+
      'html+="<span style=\\"color:#6b7280\\">display:</span><span>"+s.display+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">position:</span><span>"+s.position+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">color:</span><span>"+s.color+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">background:</span><span style=\\"max-width:180px;overflow:hidden;text-overflow:ellipsis\\">"+s.bg+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">font:</span><span>"+s.font+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">font-weight:</span><span>"+s.fw+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">padding:</span><span>"+s.padding+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">margin:</span><span>"+s.margin+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">border:</span><span>"+s.border+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">border-radius:</span><span>"+s.br+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">opacity:</span><span>"+s.opacity+"</span>";'+
      'html+="<span style=\\"color:#6b7280\\">z-index:</span><span>"+s.zIndex+"</span>";'+
      'html+="</div>";'+
      'if(s.text)html+="<div style=\\"margin-top:4px;color:#a0aec0\\">📄 "+esc_inner(s.text)+"</div>";'+
      'infoPanel.innerHTML=html;'+
      'window.parent.postMessage({source:"appPreviewInspect",info:s},"*");'+
    '},true);'+
    'function esc_inner(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}'+
    '})();';
}

function addInspectorEntry(info){
  var panel=document.getElementById('inspectInfo');
  if(!panel)return;
  if(panel.style.display==='none')panel.style.display='block';
  panel.innerHTML='<b style="color:var(--accent)">&lt;'+info.tag+'&gt;</b>'+
    (info.id?' <span style="color:var(--orange)">#'+info.id+'</span>':'')+
    (info.classes?' <span style="color:var(--green)">.'+info.classes+'</span>':'')+
    ' <span style="color:var(--text3)">'+info.size+'</span>'+
    ' <span style="color:var(--text2)">| display:'+info.display+' position:'+info.position+'</span>';
}

function toggleVisualEditor(){
  var wasActive=visualEditorActive;
  visualEditorActive=!visualEditorActive;
  var btn=document.getElementById('visEditBtn');
  btn.classList.toggle('toolbar-btn-primary',visualEditorActive);
  if(visualEditorActive){
    showToast('✏️ Визуальный редактор — клик для выбора, Alt+перетаскивание для перемещения, ПКМ для меню');
  }else if(wasActive){
    // Sync iframe content back to editor before refresh
    var frame=document.getElementById('previewFrame');
    if(frame&&frame.contentDocument&&activeTab>=0&&openTabs[activeTab]){
      var doc=frame.contentDocument;var _html='';
      try{
        var _tlb=doc.getElementById('__vistlb');if(_tlb)_tlb.remove();
        var _ss=doc.querySelectorAll('script[data-vis="1"]');
        for(var _i=0;_i<_ss.length;_i++)_ss[_i].remove();
        var _dd=doc.querySelectorAll('[data-dir]');
        for(var _i=0;_i<_dd.length;_i++)_dd[_i].remove();
        var _cm=doc.getElementById('__visCtx');if(_cm)_cm.remove();
        _html=doc.documentElement.outerHTML;
      }catch(_e){_html=doc.documentElement.outerHTML;}
      openTabs[activeTab].content=_html;
      if(useMonaco&&monacoInstance)monacoInstance.setValue(_html);
      else if(codeMirror)codeMirror.setValue(_html);
      showToast('✓ Изменения синхронизированы');
    }
  }
  refreshPreview();
}

function getVisualEditorScript(){
  return '(function(){try{'+
    'var SEL=null,TLB=null,resizeHandles=[],ctxMenu=null,dragging=false,resizing=false;'+
    'var __M=[],__multiMode=false;'+
    'var __H=[],__Hi=-1;'+
    'var __copiedStyles=null;'+
    'var __hintTimer=null;'+
    'function showShortcutHint(){'+
      'var h=document.getElementById("__visHint");if(!h){h=document.createElement("div");h.id="__visHint";h.style.cssText="position:fixed;bottom:12px;left:50%;transform:translateX(-50%);z-index:99999;background:rgba(15,20,25,0.9);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:6px 12px;color:#9ca3af;font-size:10px;font-family:sans-serif;white-space:nowrap;pointer-events:none;opacity:0;transition:opacity 0.4s";document.body.appendChild(h);}'+
      'h.innerHTML="<span style=\\"color:#e4e8ef\\">⌨</span> Ctrl+Z отмена &middot; Ctrl+Shift+Z повтор &middot; Del удалить &middot; Esc отмена &middot; Alt+перетащить переместить &middot; Shift+клик множ.выбор";'+
      'setTimeout(function(){h.style.opacity="1";},50);'+
      'if(__hintTimer)clearTimeout(__hintTimer);'+
      '__hintTimer=setTimeout(function(){h.style.opacity="0";},8000);'+
    '}'+
    'function alignEls(dir){'+
      'var targets=__M.length>0?__M:(SEL?[SEL]:[]);if(targets.length<1)return;'+
      'var rects=targets.map(function(e){return e.getBoundingClientRect();});'+
      'var cont=targets[0].parentElement.getBoundingClientRect();'+
      'if(dir==="left"){var l=Math.min.apply(null,rects.map(function(r){return r.left;}));targets.forEach(function(e,i){e.style.position=e.style.position||"relative";e.style.left=(rects[i].left-l)+"px";});}'+
      'if(dir==="center"){var cx=cont.left+cont.width/2;targets.forEach(function(e,i){e.style.position=e.style.position||"relative";e.style.left=(cx-rects[i].width/2-rects[i].left+parseInt(e.style.left||0))+"px";});}'+
      'if(dir==="right"){var rgt=Math.max.apply(null,rects.map(function(r){return r.right;}));targets.forEach(function(e,i){e.style.position=e.style.position||"relative";e.style.left=(rgt-rects[i].width-rects[i].left+parseInt(e.style.left||0))+"px";});}'+
      'if(dir==="top"){var t=Math.min.apply(null,rects.map(function(r){return r.top;}));targets.forEach(function(e,i){e.style.position=e.style.position||"relative";e.style.top=(rects[i].top-t)+"px";});}'+
      'if(dir==="middle"){var cy=cont.top+cont.height/2;targets.forEach(function(e,i){e.style.position=e.style.position||"relative";e.style.top=(cy-rects[i].height/2-rects[i].top+parseInt(e.style.top||0))+"px";});}'+
      'if(dir==="bottom"){var b=Math.max.apply(null,rects.map(function(r){return r.bottom;}));targets.forEach(function(e,i){e.style.position=e.style.position||"relative";e.style.top=(b-rects[i].height-rects[i].top+parseInt(e.style.top||0))+"px";});}'+
      'if(dir==="hdistribute"){if(targets.length<3)return;var sorted=targets.slice().sort(function(a,b){return a.getBoundingClientRect().left-b.getBoundingClientRect().left;});var firstR=sorted[0].getBoundingClientRect();var lastR=sorted[sorted.length-1].getBoundingClientRect();var gap=(lastR.right-firstR.left)/sorted.length;var cur=firstR.left;for(var i=0;i<sorted.length;i++){var r=sorted[i].getBoundingClientRect();sorted[i].style.position=sorted[i].style.position||"relative";sorted[i].style.left=(cur-r.left+parseInt(sorted[i].style.left||0))+"px";cur+=gap;}}'+
      'if(dir==="vdistribute"){if(targets.length<3)return;var sorted=targets.slice().sort(function(a,b){return a.getBoundingClientRect().top-b.getBoundingClientRect().top;});var firstR=sorted[0].getBoundingClientRect();var lastR=sorted[sorted.length-1].getBoundingClientRect();var gap=(lastR.bottom-firstR.top)/sorted.length;var cur=firstR.top;for(var i=0;i<sorted.length;i++){var r=sorted[i].getBoundingClientRect();sorted[i].style.position=sorted[i].style.position||"relative";sorted[i].style.top=(cur-r.top+parseInt(sorted[i].style.top||0))+"px";cur+=gap;}}'+
      'debounceSync();'+
    '}'+
    'function pushH(){var h=document.body.innerHTML;if(__Hi>=0&&__H[__Hi]===h)return;__H=__H.slice(0,__Hi+1);__H.push(h);if(__H.length>50)__H.shift();__Hi=__H.length-1;}'+
    'function undoH(){if(__Hi>0){__Hi--;document.body.innerHTML=__H[__Hi];TLB=null;ctxMenu=null;resizeHandles=[];window.__rhObserver=null;__M=[];__multiMode=false;SEL=null;syncHtml();showToolbar(document.body);clearSel();}}'+
    'function redoH(){if(__Hi<__H.length-1){__Hi++;document.body.innerHTML=__H[__Hi];TLB=null;ctxMenu=null;resizeHandles=[];window.__rhObserver=null;__M=[];__multiMode=false;SEL=null;syncHtml();showToolbar(document.body);clearSel();}}'+
    'function syncHtml(){'+
      'try{'+
        'var clone=document.documentElement.cloneNode(true);'+
        'var items=clone.querySelectorAll("#__vistlb,#__visHint,#__visCtx,#__vistag,script[data-vis=\\"1\\"],[data-dir]");'+
        'for(var _v=0;_v<items.length;_v++)items[_v].remove();'+
        'var h=clone.outerHTML;'+
        'window.parent.postMessage({source:"visEditorSync",html:h},"*");'+
      '}catch(e){console.error("sync:",e)}'+
    '}'+
    'function debounceSync(){clearTimeout(window.__vsync);pushH();window.__vsync=setTimeout(syncHtml,500);}'+
    'function gcs(el){var s=getComputedStyle(el);'+
      'var sb=s.boxShadow;var sx=0,sy=0,sbl=0,ssp=0,sc="#000";'+
      'if(sb&&sb!=="none"){var pm=sb.match(/(-?\\d+(\\.\\d+)?)px\\s+(-?\\d+(\\.\\d+)?)px\\s+(-?\\d+(\\.\\d+)?)px\\s+(-?\\d+(\\.\\d+)?)px\\s+(.+)/);if(pm){sx=parseFloat(pm[1]);sy=parseFloat(pm[3]);sbl=parseFloat(pm[5]);ssp=parseFloat(pm[7]);sc=pm[9].trim();}}'+
      'var bw=parseInt(s.borderWidth)||0;var bs=s.borderStyle||"solid";var bc=rgb2hex(s.borderColor);'+
      'var ts="";var tsx=0,tsy=0,tsbl=0,tsc="#000";'+
      'if(s.textShadow&&s.textShadow!=="none"){var tpm=s.textShadow.match(/(-?\\d+(\\.\\d+)?)px\\s+(-?\\d+(\\.\\d+)?)px\\s+(-?\\d+(\\.\\d+)?)px\\s+(.+)/);if(tpm){tsx=parseFloat(tpm[1]);tsy=parseFloat(tpm[3]);tsbl=parseFloat(tpm[5]);tsc=tpm[7].trim();}}'+
      'var trot=0,tscale=1;var transformOrig=s.transform;'+
      'if(transformOrig&&transformOrig!=="none"){var rpm=transformOrig.match(/rotate\\((-?\\d+(\\.\\d+)?)deg\\)/);if(rpm)trot=parseFloat(rpm[1]);var spm=transformOrig.match(/scale\\((-?\\d+(\\.\\d+)?)\\)/);if(spm)tscale=parseFloat(spm[1]);}'+
      'var attrs={};if(el.attributes){for(var ai=0;ai<el.attributes.length;ai++){var at=el.attributes[ai];if(at.name==="style"||at.name.indexOf("on")===0||at.name==="data-vis")continue;attrs[at.name]=at.value;}}'+
      'var our=s.outline||"";var ow=0,os="",oc="#000";if(our&&our!=="none"){var om=our.match(/^(\\d+)(?:px)?\\s+(\\w+)\\s+(.+)/);if(om){ow=parseInt(om[1]);os=om[2];oc=om[3];}}'+
      'return{'+
        'color:rgb2hex(s.color),'+
        'fontSize:parseInt(s.fontSize)||16,'+
        'fontWeight:s.fontWeight,'+
        'textAlign:s.textAlign,'+
        'lineHeight:s.lineHeight,'+
        'bgColor:rgb2hex(s.backgroundColor),'+
        'bgOpacity:parseFloat(s.opacity)||1,'+
        'borderRadius:parseInt(s.borderRadius)||0,'+
        'shadowX:sx,shadowY:sy,shadowBlur:sbl,shadowSpread:ssp,shadowColor:sc,'+
        'width:s.width==="auto"?"":parseInt(s.width)||"",'+
        'height:s.height==="auto"?"":parseInt(s.height)||"",'+
        'padT:parseInt(s.paddingTop)||0,padB:parseInt(s.paddingBottom)||0,padL:parseInt(s.paddingLeft)||0,padR:parseInt(s.paddingRight)||0,'+
        'marT:parseInt(s.marginTop)||0,marB:parseInt(s.marginBottom)||0,marL:parseInt(s.marginLeft)||0,marR:parseInt(s.marginRight)||0,'+
        'borderWidth:bw,borderStyle:bs,borderColor:bc,'+
        'textShadowX:tsx,textShadowY:tsy,textShadowBlur:tsbl,textShadowColor:tsc,'+
        'transformRotate:trot,transformScale:tscale,transformOrig:transformOrig,'+
        'display:s.display,'+
        'position:s.position,'+
        'top:s.top,"left":s.left,"right":s.right,"bottom":s.bottom,'+
        'overflow:s.overflow,'+
        'fontFamily:s.fontFamily,'+
        'cursor:s.cursor,'+
        'zIndex:s.zIndex,'+
        'boxSizing:s.boxSizing,'+
        'gap:s.gap,'+
        'letterSpacing:s.letterSpacing,'+
        'flexDirection:s.flexDirection,flexWrap:s.flexWrap,justifyContent:s.justifyContent,alignItems:s.alignItems,'+
        'outlineWidth:ow,outlineStyle:os,outlineColor:oc,'+
        'backgroundSize:s.backgroundSize,backgroundRepeat:s.backgroundRepeat,'+
        'textTransform:s.textTransform,textDecoration:s.textDecoration,'+
        'visibility:s.visibility,'+
        'wordSpacing:s.wordSpacing,whiteSpace:s.whiteSpace,'+
        'attrs:attrs'+
      '};'+
    '}'+
    'function rgb2hex(rgb){'+
      'if(!rgb||rgb==="transparent"||rgb==="rgba(0, 0, 0, 0)")return"#ffffff";'+
      'var m=rgb.match(/^rgb\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*\\)$/);'+
      'if(m)return"#"+("0"+parseInt(m[1]).toString(16)).slice(-2)+("0"+parseInt(m[2]).toString(16)).slice(-2)+("0"+parseInt(m[3]).toString(16)).slice(-2);'+
      'var ma=rgb.match(/^rgba\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*([\\d.]+)\\s*\\)$/);'+
      'if(ma)return"#"+("0"+parseInt(ma[1]).toString(16)).slice(-2)+("0"+parseInt(ma[2]).toString(16)).slice(-2)+("0"+parseInt(ma[3]).toString(16)).slice(-2);'+
      'return"#000000";'+
    '}'+
    'function clearSel(){__M.forEach(function(m){if(m&&m.style){m.style.outline="";m.style.outlineOffset="";}});__M=[];__multiMode=false;if(SEL){SEL.style.outline="";SEL.style.outlineOffset="";SEL=null;}removeResizeHandles();if(TLB){TLB.style.display="flex";var tg=document.getElementById("__vistag");if(tg)tg.textContent="Выберите элемент";}}'+


    'function selectEl(el,multi){'+
      'if(multi){'+
        'var idx=__M.indexOf(el);'+
        'if(idx>=0){__M.splice(idx,1);el.style.outline="";el.style.outlineOffset="";if(__M.length===0)__multiMode=false;}'+
        'else{__M.push(el);el.style.outline="2px dotted #6366f1";el.style.outlineOffset="-2px";__multiMode=true;}'+
        'if(__multiMode&&TLB&&__M.length>0){TLB.style.display="flex";var tg=document.getElementById("__vistag");if(tg)tg.textContent=__M.length+" selected";}'+
        'else if(TLB&&!__multiMode){TLB.style.display="flex";var tg=document.getElementById("__vistag");if(tg)tg.textContent="Выберите элемент";}'+
        'return;'+
      '}'+
      'clearSel();SEL=el;el.style.outline="2px solid #6366f1";el.style.outlineOffset="-2px";showResizeHandles(el);showToolbar(el);'+
    '}'+
    'function showToolbar(el){'+
      'if(!TLB){var _old=document.getElementById("__vistlb");if(_old)_old.remove();TLB=document.createElement("div");TLB.id="__vistlb";TLB.style.cssText="position:fixed;z-index:99998;right:0;top:0;height:100%;width:195px;background:rgba(15,20,25,0.95);backdrop-filter:blur(12px);border-left:1px solid rgba(255,255,255,0.1);padding:8px 6px;display:none;flex-direction:column;gap:2px;box-shadow:-4px 0 24px rgba(0,0,0,0.5);font-size:11px;font-family:sans-serif;overflow-y:auto";'+
      'TLB.innerHTML='+
        '"<button data-act=\\"style\\" style=\\"display:block;width:100%;padding:7px 8px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;cursor:pointer;font-size:12px;font-weight:600;margin:2px 0;text-align:left\\" title=\\"Стили (палитра, тени, отступы)\\">🎨 Стиль...</button>"+'+
          '"<button data-act=\\"select-body\\" style=\\"display:block;width:100%;padding:5px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px;margin:1px 0;text-align:left\\" title=\\"Выбрать элемент &lt;body&gt;\\">🧍 &lt;body&gt;</button>"+'+
        '"<div style=\\"height:1px;background:rgba(255,255,255,0.08);margin:4px 0\\"></div>"+'+
        '"<div style=\\"display:flex;gap:2px;margin:2px 0\\">"+"<button data-act=\\"add\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px\\" title=\\"Добавить элемент\\">📦+</button>"+"<button data-act=\\"delete\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#ef4444;cursor:pointer;font-size:10px\\" title=\\"Удалить элемент\\">🗑</button>"+"<button data-act=\\"duplicate\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px\\" title=\\"Дублировать\\">📋</button>"+"<button data-act=\\"reset\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px\\" title=\\"Сбросить стили\\">↺</button>"+"</div>"+'+
        '"<div style=\\"display:flex;gap:2px;margin:2px 0\\">"+"<button data-act=\\"undo\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px\\" title=\\"Отменить Ctrl+Z\\">↩ Отм.</button>"+"<button data-act=\\"redo\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px\\" title=\\"Повторить Ctrl+Shift+Z\\">↪ Повт.</button>"+"</div>"+'+
        '"<div style=\\"display:flex;gap:2px;margin:2px 0\\">"+"<button data-act=\\"front\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px\\" title=\\"На передний план\\">↑ Наверх</button>"+"<button data-act=\\"back\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px\\" title=\\"На задний план\\">↓ Вниз</button>"+"</div>"+'+
        '"<div style=\\"height:1px;background:rgba(255,255,255,0.08);margin:4px 0\\"></div>"+'+
        '"<div style=\\"display:flex;gap:2px;flex-wrap:wrap;justify-content:center;margin:2px 0\\">"+"<button data-act=\\"align-left\\" style=\\"flex:1;min-width:28px;padding:4px 0;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"Выровнять влево\\">⬅</button>"+"<button data-act=\\"align-center\\" style=\\"flex:1;min-width:28px;padding:4px 0;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"По центру\\">⬌</button>"+"<button data-act=\\"align-right\\" style=\\"flex:1;min-width:28px;padding:4px 0;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"Выровнять вправо\\">➡</button>"+"<button data-act=\\"align-top\\" style=\\"flex:1;min-width:28px;padding:4px 0;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"По верху\\">⬆</button>"+"<button data-act=\\"align-middle\\" style=\\"flex:1;min-width:28px;padding:4px 0;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"По середине\\">⬍</button>"+"<button data-act=\\"align-bottom\\" style=\\"flex:1;min-width:28px;padding:4px 0;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"По низу\\">⬇</button>"+"</div>"+'+
        '"<div style=\\"height:1px;background:rgba(255,255,255,0.08);margin:4px 0\\"></div>"+'+
        '"<div style=\\"display:flex;gap:2px;margin:2px 0\\">"+"<button data-act=\\"copy-styles\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"Копировать стили\\">📋 Коп.</button>"+"<button data-act=\\"paste-styles\\" style=\\"flex:1;padding:5px 2px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:9px\\" title=\\"Вставить стили\\">📌 Вст.</button>"+"</div>"+'+
        '"<div style=\\"height:1px;background:rgba(255,255,255,0.08);margin:4px 0\\"></div>"+'+
        '"<button data-act=\\"image-lib\\" style=\\"display:block;width:100%;padding:5px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:10px;margin:1px 0;text-align:left\\" title=\\"Библиотека изображений\\">🖼 Изображения</button>"+'+
        '"<div style=\\"flex:1\\"></div>"+'+
        '"<div style=\\"padding:6px 4px;border-top:1px solid rgba(255,255,255,0.08);text-align:center;color:#6b7280;font-size:10px\\" id=\\"__vistag\\"></div>"'+
      ';'+
      'showShortcutHint();'+
      'TLB.addEventListener("click",function(e){'+
        'var b=e.target.closest("[data-act]");if(!b)return;var a=b.dataset.act;if(a!=="style"&&a!=="select-body"&&!SEL)return;'+
        'if(a==="select-body"){selectEl(document.body);}'+
        'else if(a==="style"){if(!SEL){window.parent.postMessage({source:"visEditorToast",msg:"\u26a0\ufe0f \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u044d\u043b\u0435\u043c\u0435\u043d\u0442"},"*");}else{try{window.parent.postMessage({source:"visEditorStyle",action:"edit",current:gcs(SEL)},"*");}catch(er){window.parent.postMessage({source:"visEditorToast",msg:"\u041e\u0448\u0438\u0431\u043a\u0430: "+er.message},"*");}}}'+
        'else if(a==="add"){addElDialog();}'+
        'else if(a==="delete"){if(confirm("Удалить элемент "+SEL.tagName.toLowerCase()+"?")){var p=SEL.parentNode;SEL.remove();clearSel();debounceSync();}}'+
        'else if(a==="duplicate"){var c=SEL.cloneNode(true);SEL.parentNode.insertBefore(c,SEL.nextSibling);selectEl(c);debounceSync();}'+
        'else if(a==="undo"){undoH();}'+
        'else if(a==="redo"){redoH();}'+
        'else if(a==="front"){if(__M.length>0){__M.forEach(function(m){m.style.zIndex=9999;});}else if(SEL){SEL.style.zIndex=9999;}}'+
        'else if(a==="back"){if(__M.length>0){__M.forEach(function(m){m.style.zIndex=1;});}else if(SEL){SEL.style.zIndex=1;}}'+
        'else if(a==="reset"){SEL.removeAttribute("style");debounceSync();}'+
        'else if(a.indexOf("align-")===0){alignEls(a.replace("align-",""));}'+
        'else if(a==="copy-styles"){__copiedStyles=gcs(SEL);window.parent.postMessage({source:"visEditorToast",msg:"✅ Стили скопированы"},"*");}'+
        'else if(a==="paste-styles"){if(!__copiedStyles)return;var targs=__M.length>0?__M:[SEL];targs.forEach(function(e){e.style.color=__copiedStyles.color;e.style.fontSize=__copiedStyles.fontSize+"px";e.style.fontWeight=__copiedStyles.fontWeight;e.style.textAlign=__copiedStyles.textAlign;e.style.lineHeight=__copiedStyles.lineHeight;e.style.backgroundColor=__copiedStyles.bgColor;e.style.opacity=__copiedStyles.bgOpacity;e.style.borderRadius=__copiedStyles.borderRadius+"px";e.style.boxShadow=__copiedStyles.shadowX+"px "+__copiedStyles.shadowY+"px "+__copiedStyles.shadowBlur+"px "+__copiedStyles.shadowSpread+"px "+__copiedStyles.shadowColor;e.style.padding=__copiedStyles.padT+"px "+__copiedStyles.padR+"px "+__copiedStyles.padB+"px "+__copiedStyles.padL+"px";e.style.margin=__copiedStyles.marT+"px "+__copiedStyles.marR+"px "+__copiedStyles.marB+"px "+__copiedStyles.marL+"px";});selectEl(SEL);debounceSync();}'+
        'else if(a==="image-lib"){window.parent.postMessage({source:"visEditorImgLib",action:"open"},"*");}'+
      '});'+
      'document.body.appendChild(TLB);'+
      '}'+
      'var tg=document.getElementById("__vistag");if(tg)tg.textContent="<"+el.tagName.toLowerCase()+">"+(el.id?" #"+el.id:"")+(el.className?" ."+el.className.split(" ").join("."):"");'+
      'TLB.style.display="flex";'+
    '}'+
    'function showResizeHandles(el){'+
      'removeResizeHandles();'+
      'var dirs=[{d:"nw",c:"nw-resize",l:-6,t:-6},{d:"n",c:"n-resize",l:"50%",t:-6,x:-3},{d:"ne",c:"ne-resize",l:"calc(100% - 6px)",t:-6},{d:"e",c:"e-resize",l:"calc(100% - 6px)",t:"50%",y:-3},{d:"se",c:"se-resize",l:"calc(100% - 6px)",t:"calc(100% - 6px)"},{d:"s",c:"s-resize",l:"50%",t:"calc(100% - 6px)",x:-3},{d:"sw",c:"sw-resize",l:-6,t:"calc(100% - 6px)"},{d:"w",c:"w-resize",l:-6,t:"50%",y:-3}];'+
      'dirs.forEach(function(d){'+
        'var h=document.createElement("div");'+
        'h.style.cssText="position:fixed;z-index:99997;width:12px;height:12px;background:#6366f1;border:2px solid #fff;border-radius:3px;cursor:"+d.c+";pointer-events:all";'+
        'h.dataset.dir=d.d;'+
        'h.addEventListener("mousedown",function(ev){'+
          'ev.preventDefault();ev.stopPropagation();'+
          'var startX=ev.clientX,startY=ev.clientY;'+
          'var stw=el.offsetWidth,sth=el.offsetHeight;'+
          'var stl=el.offsetLeft||0,stt=el.offsetTop||0;'+
          'var dir=d.d;'+
          'function onMove(me){'+
            'var dx=me.clientX-startX,dy=me.clientY-startY;'+
            'if(dir.indexOf("e")>=0)el.style.width=Math.max(20,stw+dx)+"px";'+
            'if(dir.indexOf("w")>=0){el.style.width=Math.max(20,stw-dx)+"px";el.style.left=(stl+dx)+"px";}'+
            'if(dir.indexOf("s")>=0)el.style.height=Math.max(10,sth+dy)+"px";'+
            'if(dir.indexOf("n")>=0){el.style.height=Math.max(10,sth-dy)+"px";el.style.top=(stt+dy)+"px";}'+
            'el.style.position=el.style.position||"relative";'+
            'showToolbar(el);showResizeHandles(el);'+
          '}'+
          'function onUp(){document.removeEventListener("mousemove",onMove);document.removeEventListener("mouseup",onUp);debounceSync();}'+
          'document.addEventListener("mousemove",onMove);document.addEventListener("mouseup",onUp);'+
        '});'+
        'resizeHandles.push(h);document.body.appendChild(h);'+
        'function posH(){'+
          'var r=el.getBoundingClientRect();'+
          'var l=d.l,t=d.t;'+
          'if(d.x)l="calc("+d.l+" + "+d.x+"px)";'+
          'if(d.y)t="calc("+d.t+" + "+d.y+"px)";'+
          'if(typeof l==="number")l=r.left+r.width/2+l;'+
          'if(typeof t==="number")t=r.top+r.height/2+t;'+
          'if(typeof l==="string"&&l.indexOf("calc")<0){if(l.indexOf("%")>=0)l=r.left+parseFloat(l)/100*r.width-6;else l=parseFloat(l);}'+
          'if(typeof t==="string"&&t.indexOf("calc")<0){if(t.indexOf("%")>=0)t=r.top+parseFloat(t)/100*r.height-6;else t=parseFloat(t);}'+
          'h.style.left=(typeof l==="number"?l:r.left+parseFloat(l.split("+")[0]||"0")+(parseFloat(l.split("+")[1]||"0")))+"px";'+
          'h.style.top=(typeof t==="number"?t:r.top+parseFloat(t.split("+")[0]||"0")+(parseFloat(t.split("+")[1]||"0")))+"px";'+
        '}'+
        'posH();window.__rhObserver=window.__rhObserver||new MutationObserver(function(){resizeHandles.forEach(function(hi){if(hi.parentNode){var dir=hi.dataset.dir;var ri=SEL.getBoundingClientRect();hi.style.left=(dir==="nw"?ri.left-6:dir==="n"?ri.left+ri.width/2-6:dir==="ne"?ri.right-6:dir==="e"?ri.right-6:dir==="se"?ri.right-6:dir==="s"?ri.left+ri.width/2-6:dir==="sw"?ri.left-6:ri.left-6)+"px";'+
        'hi.style.top=(dir==="nw"?ri.top-6:dir==="n"?ri.top-6:dir==="ne"?ri.top-6:dir==="e"?ri.top+ri.height/2-6:dir==="se"?ri.bottom-6:dir==="s"?ri.bottom-6:dir==="sw"?ri.bottom-6:ri.top+ri.height/2-6)+"px";}});});'+
        'window.__rhObserver.observe(el,{attributes:true,childList:true,subtree:true});'+
      '});'+
    '}'+
    'function removeResizeHandles(){'+
      'resizeHandles.forEach(function(h){h.remove();});'+
      'resizeHandles=[];'+
      'if(window.__rhObserver){window.__rhObserver.disconnect();window.__rhObserver=null;}'+
    '}'+
    'function addElDialog(){'+
      'var tags=['+
        '"div","span","p","a","br","hr","wbr",'+
        '"h1","h2","h3","h4","h5","h6",'+
        '"ul","ol","li","dl","dt","dd",'+
        '"table","thead","tbody","tfoot","tr","th","td","col","colgroup","caption",'+
        '"form","input","textarea","button","select","option","optgroup","label","fieldset","legend","datalist","output","progress","meter",'+
        '"img","picture","source","figure","figcaption","map","area",'+
        '"video","audio","track","canvas","svg",'+
        '"header","footer","nav","main","aside","section","article",'+
        '"details","summary","dialog","menu",'+
        '"blockquote","cite","q","code","pre","kbd","samp","var","abbr","address","bdo","bdi","ruby","rt","rp",'+
        '"em","strong","small","sub","sup","mark","ins","del","s","u","i","b","dfn","time","data",'+
        '"iframe","embed","object","param",'+
        '"div"'+ // duplicate as default
      '];'+
      'var ov=document.createElement("div");'+
      'ov.style.cssText="position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)";'+
      'ov.innerHTML="<div style=\\"background:rgba(15,20,25,0.95);border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:20px 24px;max-width:400px;width:90%;color:#e4e8ef;font-family:sans-serif\\">"+'+
        '"<h3 style=\\"margin:0 0 12px;font-size:16px;font-weight:700\\">📦 Добавить элемент</h3>"+'+
        '"<label style=\\"font-size:11px;color:#9ca3af;display:block;margin-bottom:4px\\">Тег</label>"+'+
        '"<select id=\\"__addTag\\" style=\\"width:100%;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#e4e8ef;font-size:13px;margin-bottom:8px;outline:none\\">"+tags.map(function(t){return"<option value=\\""+t+"\\">"+t+"</option>"}).join("")+"</select>"+'+
        '"<label style=\\"font-size:11px;color:#9ca3af;display:block;margin-bottom:4px\\">Куда добавить</label>"+'+
        '"<select id=\\"__addWhere\\" style=\\"width:100%;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.05);color:#e4e8ef;font-size:13px;margin-bottom:16px;outline:none\\"><option value=\\"after\\">После выбранного</option><option value=\\"before\\">До выбранного</option><option value=\\"child\\">Внутрь (в конец)</option><option value=\\"prepend\\">Внутрь (в начало)</option></select>"+'+
        '"<div style=\\"display:flex;gap:8px\\"><button id=\\"__addOk\\" style=\\"flex:1;padding:8px;border-radius:8px;border:none;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;cursor:pointer;font-size:13px;font-weight:600\\">Добавить</button>"+'+
        '"<button id=\\"__addCancel\\" style=\\"flex:1;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#e4e8ef;cursor:pointer;font-size:13px\\">Отмена</button></div>"+'+
      '"</div>";'+
      'document.body.appendChild(ov);'+
      'document.getElementById("__addOk").onclick=function(){'+
        'var tag=document.getElementById("__addTag").value;'+
        'var where=document.getElementById("__addWhere").value;'+
        'var el=document.createElement(tag);'+
        'el.textContent="<"+tag+">";'+
        'if(tag==="img"){el.src="https://placehold.co/100";el.alt="placeholder";el.textContent="";}'+
        'if(where==="after"&&SEL&&SEL.parentNode){SEL.parentNode.insertBefore(el,SEL.nextSibling);}'+
        'else if(where==="before"&&SEL&&SEL.parentNode){SEL.parentNode.insertBefore(el,SEL);}'+
        'else if(where==="child"&&SEL){SEL.appendChild(el);}'+
        'else if(where==="prepend"&&SEL){SEL.insertBefore(el,SEL.firstChild);}'+
        'ov.remove();selectEl(el);debounceSync();'+
      '};'+
      'document.getElementById("__addCancel").onclick=function(){ov.remove();};'+
      'ov.onclick=function(e){if(e.target===ov)ov.remove();};'+
    '}'+
    'function showCtxMenu(e){'+
      'e.preventDefault();if(ctxMenu)ctxMenu.remove();'+
      'ctxMenu=document.createElement("div");'+
      'ctxMenu.style.cssText="position:fixed;z-index:99999;background:rgba(15,20,25,0.96);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:4px 0;min-width:160px;box-shadow:0 8px 32px rgba(0,0,0,0.5);font-size:12px;font-family:sans-serif";'+
      'ctxMenu.innerHTML='+
        '"<div data-ctx=\\"child\\" style=\\"padding:6px 14px;cursor:pointer;color:#e4e8ef;display:flex;align-items:center;gap:8px\\"><span>➕</span> Добавить дочерний</div>"+'+
        '"<div data-ctx=\\"sibling\\" style=\\"padding:6px 14px;cursor:pointer;color:#e4e8ef;display:flex;align-items:center;gap:8px\\"><span>📥</span> Добавить после</div>"+'+
        '"<div data-ctx=\\"duplicate\\" style=\\"padding:6px 14px;cursor:pointer;color:#e4e8ef;display:flex;align-items:center;gap:8px\\"><span>📋</span> Дублировать</div>"+'+
        '"<div style=\\"height:1px;background:rgba(255,255,255,0.08);margin:4px 0\\"></div>"+'+
        '"<div data-ctx=\\"copy\\" style=\\"padding:6px 14px;cursor:pointer;color:#e4e8ef;display:flex;align-items:center;gap:8px\\"><span>📄</span> Копировать HTML</div>"+'+
        '"<div style=\\"height:1px;background:rgba(255,255,255,0.08);margin:4px 0\\"></div>"+'+
        '"<div data-ctx=\\"delete\\" style=\\"padding:6px 14px;cursor:pointer;color:#ef4444;display:flex;align-items:center;gap:8px\\"><span>🗑</span> Удалить</div>"'+
      ';'+
      'ctxMenu.style.left=Math.min(e.clientX,window.innerWidth-180)+"px";'+
      'ctxMenu.style.top=Math.min(e.clientY,window.innerHeight-280)+"px";'+
      'ctxMenu.addEventListener("click",function(ev){'+
        'var item=ev.target.closest("[data-ctx]");if(!item||!SEL)return;'+
        'var act=item.dataset.ctx;'+
        'if(act==="child"){var c=document.createElement("div");c.textContent="new child";SEL.appendChild(c);selectEl(c);debounceSync();}'+
        'else if(act==="sibling"){var s=document.createElement("div");s.textContent="new sibling";if(SEL.parentNode)SEL.parentNode.insertBefore(s,SEL.nextSibling);selectEl(s);debounceSync();}'+
        'else if(act==="duplicate"){var d=SEL.cloneNode(true);SEL.parentNode.insertBefore(d,SEL.nextSibling);selectEl(d);debounceSync();}'+
        'else if(act==="copy"){navigator.clipboard.writeText(SEL.outerHTML).then(function(){window.parent.postMessage({source:"visEditorToast",msg:"HTML скопирован"},"*");});}'+
        'else if(act==="delete"){SEL.remove();clearSel();debounceSync();}'+
        'ctxMenu.remove();ctxMenu=null;'+
      '});'+
      'document.body.appendChild(ctxMenu);'+
      'document.addEventListener("click",function rmCtx(){if(ctxMenu){ctxMenu.remove();ctxMenu=null;}document.removeEventListener("click",rmCtx);},{once:true});'+
    '}'+
    'function initVis(){showToolbar(document.body);clearSel();}if(document.readyState==="complete"||document.readyState==="interactive")initVis();else document.addEventListener("DOMContentLoaded",initVis);'+
    'document.addEventListener("click",function(e){'+
      'if(e.shiftKey){selectEl(e.target,true);return;}'+
      'var el=e.target;'+
      'if(el===TLB||TLB&&TLB.contains(el))return;'+
      'if(el.isContentEditable)return;'+
      'if(el===document.documentElement||el===document.body)return;'+
      'if(ctxMenu&&(el===ctxMenu||ctxMenu.contains(el)))return;'+
      'selectEl(el);'+
    '},true);'+
    'document.addEventListener("dblclick",function(e){'+
      'var el=e.target;'+
      'if(el===TLB||TLB&&TLB.contains(el))return;'+
      'if(ctxMenu&&(el===ctxMenu||ctxMenu.contains(el)))return;'+
      'if(el===document.documentElement||el===document.body)return;'+
      'if(el.isContentEditable)return;'+
      'e.preventDefault();'+
      'el.contentEditable="true";'+
      'el.focus();'+
      'el.style.outline="2px dashed #6366f1";el.style.outlineOffset="-2px";'+
      'el.addEventListener("blur",function(){el.contentEditable="false";el.style.outline="";el.style.outlineOffset="";debounceSync();},{once:true});'+
    '},true);'+
    'document.addEventListener("keydown",function(e){'+
      'if(e.key==="Escape"){clearSel();}'+
      'if(e.key==="Delete"&&SEL&&!e.target.isContentEditable){SEL.remove();clearSel();debounceSync();}'+
      'if(e.ctrlKey&&e.key==="z"){if(e.shiftKey)redoH();else undoH();e.preventDefault();}'+
    '},true);'+
    'document.addEventListener("contextmenu",function(e){'+
      'e.preventDefault();'+
      'var el=e.target;'+
      'if(el===TLB||TLB&&TLB.contains(el))return;'+
      'if(el===document.documentElement||el===document.body)return;'+
      'if(el.isContentEditable)return;'+
      'selectEl(el);showCtxMenu(e);'+
    '},true);'+
    'document.addEventListener("mousedown",function(e){'+
      'if(!e.altKey||!SEL)return;'+
      'var el=e.target;'+
      'if(el===TLB||TLB&&TLB.contains(el))return;'+
      'if(!SEL.contains(el)&&el!==SEL)return;'+
      'dragging=true;'+
      'var sx=e.clientX,sy=e.clientY;'+
      'var st=SEL.style.position||getComputedStyle(SEL).position;'+
      'if(st==="static"){SEL.style.position="relative";}'+
      'var sl=parseInt(SEL.style.left)||0,stt=parseInt(SEL.style.top)||0;'+
      'function onMove(me){'+
        'SEL.style.left=(sl+me.clientX-sx)+"px";'+
        'SEL.style.top=(stt+me.clientY-sy)+"px";'+
        'showToolbar(SEL);showResizeHandles(SEL);'+
      '}'+
      'function onUp(){dragging=false;document.removeEventListener("mousemove",onMove);document.removeEventListener("mouseup",onUp);debounceSync();}'+
      'document.addEventListener("mousemove",onMove);document.addEventListener("mouseup",onUp);'+
    '},true);'+
    'document.addEventListener("dragover",function(e){e.preventDefault();},false);'+
    'document.addEventListener("drop",function(e){'+
      'e.preventDefault();var files=e.dataTransfer.files;if(!files||!files.length)return;'+
      'var el=e.target;if(el.isContentEditable||el===TLB||TLB&&TLB.contains(el))return;'+
      'for(var fi=0;fi<files.length;fi++){'+
        'var file=files[fi];var ext=file.name.split(".").pop().toLowerCase();'+
        'if(["png","jpg","jpeg","gif","svg","webp","bmp","ico"].indexOf(ext)>=0){'+
          'var reader=new FileReader();reader.onload=function(ev){'+
            'var img=document.createElement("img");img.src=ev.target.result;img.style.maxWidth="100%";img.alt=file.name;'+
            'if(el.tagName==="BODY"||el===SEL)el.appendChild(img);else if(SEL)SEL.parentNode.insertBefore(img,SEL.nextSibling);'+
            'selectEl(img);debounceSync();'+
          '};reader.readAsDataURL(file);'+
        '}else if(ext==="css"){'+
          'var r2=new FileReader();r2.onload=function(ev){var l=document.createElement("link");l.rel="stylesheet";l.href=URL.createObjectURL(file);document.head.appendChild(l);debounceSync();};r2.readAsDataURL(file);'+
        '}else if(ext==="js"){'+
          'var r3=new FileReader();r3.onload=function(ev){var s=document.createElement("script");s.src=URL.createObjectURL(file);document.head.appendChild(s);debounceSync();};r3.readAsDataURL(file);'+
        '}'+
      '}'+
    '},false);'+
    'function applyStyleData(d){'+
      'var targets=__M.length>0?__M:(SEL?[SEL]:[]);if(!targets.length)return;'+
      'targets.forEach(function(el){'+
        'el.style.color=d.color;'+
        'el.style.fontSize=d.fontSize+"px";'+
        'el.style.fontWeight=d.fontWeight;'+
        'el.style.textAlign=d.textAlign;'+
        'el.style.lineHeight=d.lineHeight;'+
        'el.style.backgroundColor=d.bgColor;'+
        'el.style.opacity=d.bgOpacity;'+
        'el.style.borderRadius=d.borderRadius+"px";'+
        'el.style.boxShadow=d.shadowX+"px "+d.shadowY+"px "+d.shadowBlur+"px "+d.shadowSpread+"px "+d.shadowColor;'+
        'var w=Number(d.width);if(d.width&&!isNaN(w))el.style.width=w+"px";else if(d.width)el.style.width=d.width;'+
        'var h=Number(d.height);if(d.height&&!isNaN(h))el.style.height=h+"px";else if(d.height)el.style.height=d.height;'+
        'el.style.padding=d.padT+"px "+d.padR+"px "+d.padB+"px "+d.padL+"px";'+
        'el.style.margin=d.marT+"px "+d.marR+"px "+d.marB+"px "+d.marL+"px";'+
        'if(d.borderWidth){el.style.borderWidth=d.borderWidth+"px";el.style.borderStyle=d.borderStyle||"solid";el.style.borderColor=d.borderColor;}'+
        'if(d.textShadowX!==undefined)el.style.textShadow=d.textShadowX+"px "+d.textShadowY+"px "+d.textShadowBlur+"px "+d.textShadowColor;'+
        'if(d.transformRotate!==undefined){var tf="rotate("+d.transformRotate+"deg)"+(d.transformScale?" scale("+d.transformScale+")":"");if(d.transformOrig&&d.transformOrig!=="none"){var rest=d.transformOrig.replace(/rotate\\([^)]*\\)\\s*/g,"").replace(/scale\\([^)]*\\)\\s*/g,"").trim();if(rest)tf=rest+" "+tf;}el.style.transform=tf;}'+
        'if(d.display)el.style.display=d.display;'+
        'if(d.position)el.style.position=d.position;'+
        'if(d.top!==undefined&&d.top!=="")el.style.top=d.top;'+
        'if(d.left!==undefined&&d.left!=="")el.style.left=d.left;'+
        'if(d.right!==undefined&&d.right!=="")el.style.right=d.right;'+
        'if(d.bottom!==undefined&&d.bottom!=="")el.style.bottom=d.bottom;'+
        'if(d.overflow)el.style.overflow=d.overflow;'+
        'if(d.fontFamily)el.style.fontFamily=d.fontFamily;'+
        'if(d.cursor)el.style.cursor=d.cursor;'+
        'if(d.zIndex)el.style.zIndex=d.zIndex;'+
        'if(d.boxSizing)el.style.boxSizing=d.boxSizing;'+
        'if(d.gap)el.style.gap=d.gap;'+
        'if(d.letterSpacing!==undefined)el.style.letterSpacing=d.letterSpacing+"px";'+
        'if(d.flexDirection)el.style.flexDirection=d.flexDirection;'+
        'if(d.flexWrap)el.style.flexWrap=d.flexWrap;'+
        'if(d.justifyContent)el.style.justifyContent=d.justifyContent;'+
        'if(d.alignItems)el.style.alignItems=d.alignItems;'+
        'if(d.outlineWidth){el.style.outlineWidth=d.outlineWidth+"px";el.style.outlineStyle=d.outlineStyle||"solid";el.style.outlineColor=d.outlineColor;}'+
        'if(d.backgroundSize)el.style.backgroundSize=d.backgroundSize;'+
        'if(d.backgroundRepeat)el.style.backgroundRepeat=d.backgroundRepeat;'+
        'if(d.textTransform)el.style.textTransform=d.textTransform;'+
        'if(d.textDecoration)el.style.textDecoration=d.textDecoration;'+
        'if(d.visibility)el.style.visibility=d.visibility;'+
        'if(d.wordSpacing!==undefined)el.style.wordSpacing=d.wordSpacing+"px";'+
        'if(d.whiteSpace)el.style.whiteSpace=d.whiteSpace;'+
      '});'+
    '}'+
    'window.addEventListener("message",function(e){'+
      'if(e.data&&e.data.source==="visEditorResult"){'+
        'if(e.data.status==="ok"||e.data.status==="preview"){'+
          'applyStyleData(e.data.data);'+
          'if(e.data.status==="ok"){if(SEL)showToolbar(SEL);showResizeHandles(SEL);debounceSync();}'+
        '}else if(e.data.status==="image"){'+
          'var el=document.createElement("img");el.src=e.data.url;el.alt=e.data.alt||"";el.style.maxWidth="100%";'+
          'if(SEL&&SEL.tagName!=="BODY"){SEL.parentNode.insertBefore(el,SEL.nextSibling);selectEl(el);debounceSync();}'+
          'else if(SEL){SEL.appendChild(el);selectEl(el);debounceSync();}'+
        '}'+
      '}'+
    '},false);'+
    '}catch(e){console.error("vis:",e)}})();';
}

function exportPreviewHtml(){
  var frame=document.getElementById('previewFrame');
  if(!frame||!frame.contentDocument){showToast('Превью не активно','error');return;}
  var doc=frame.contentDocument;
  try{
    var tlb=doc.getElementById('__vistlb');if(tlb)tlb.remove();
    var ss=doc.querySelectorAll('script[data-vis="1"]');
    for(var i=0;i<ss.length;i++)ss[i].remove();
    var dd=doc.querySelectorAll('[data-dir]');
    for(var i=0;i<dd.length;i++)dd[i].remove();
    var cm=doc.getElementById('__visCtx');if(cm)cm.remove();
  }catch(e){}
  var html=doc.documentElement.outerHTML;
  navigator.clipboard.writeText(html).then(function(){showToast('✓ HTML скопирован в буфер');}).catch(function(){showToast('Ошибка копирования','error');});
}

function toggleTerminal(){
  const panel=document.getElementById('terminalPanel');
  panel.classList.toggle('show');
  setTimeout(()=>{
    if(terminalFitAddon)terminalFitAddon.fit();
    if(terminal)terminal.focus();
  },150);
}
function openInDesktop(){window.location.href='/desktop';}

function exportZip(){
  if(!files||!files.length){showToast('Нет файлов для экспорта','error');return;}
  if(typeof JSZip==='undefined'){showToast('JSZip не загружен','error');return;}
  var zip=new JSZip();
  files.forEach(function(f){
    if(f.path&&!f.is_dir)zip.file(f.path,f.content||'');
  });
  zip.generateAsync({type:'blob'}).then(function(blob){
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download=extName+'.zip';
    document.body.appendChild(a);a.click();
    setTimeout(function(){document.body.removeChild(a);URL.revokeObjectURL(a.href);},100);
    showToast('✓ Архив '+extName+'.zip скачан');
  }).catch(function(e){showToast('Ошибка: '+e.message,'error');});
}

function beautifyCode(){
  if(activeTab<0){showToast('Нет открытого файла','error');return;}
  var content='';
  if(useMonaco&&monacoInstance)content=monacoInstance.getValue();
  else if(codeMirror)content=codeMirror.getValue();
  else content=openTabs[activeTab].content;
  if(!content.trim()){showToast('Файл пуст','error');return;}
  var ext=openTabs[activeTab].name.split('.').pop().toLowerCase();
  try{
    var result=content;
    if(['html','htm'].indexOf(ext)>=0){
      result=beautifyHTML(content);
    }else if(['js','javascript'].indexOf(ext)>=0){
      result=beautifyJS(content);
    }else if(['css'].indexOf(ext)>=0){
      result=beautifyCSS(content);
    }else if(['json'].indexOf(ext)>=0){
      result=JSON.stringify(JSON.parse(content),null,2);
    }else{
      showToast('Beautifier не поддерживается для .'+ext,'error');return;
    }
    if(useMonaco&&monacoInstance)monacoInstance.setValue(result);
    else if(codeMirror)codeMirror.setValue(result);
    openTabs[activeTab].content=result;
    showToast('✓ Код отформатирован');
  }catch(e){showToast('Ошибка форматирования: '+e.message,'error');}
}
function beautifyHTML(html){
  var tags=html.split(/(<[^>]*>)/);
  var result='',indent=0,selfClosing=/^(area|base|br|col|embed|hr|img|input|link|meta|param|source|track|wbr)$/i;
  for(var i=0;i<tags.length;i++){
    var t=tags[i];
    if(!t||t==='')continue;
    if(t.startsWith('<')){
      var isClose=t.startsWith('</');
      var tagName=isClose?t.match(/<\/(\w+)/):t.match(/<(\w+)/);
      if(!tagName){result+=t;continue;}
      var name=tagName[1].toLowerCase();
      if(isClose){indent--;result+='\n'+'  '.repeat(Math.max(0,indent))+t;}
      else if(selfClosing.test(name)||t.endsWith('/>')){
        result+='\n'+'  '.repeat(indent)+t;
      }else{
        result+='\n'+'  '.repeat(indent)+t;
        indent++;
      }
    }else{
      var trimmed=t.trim();
      if(trimmed){
        result+='\n'+'  '.repeat(indent)+trimmed;
      }
    }
  }
  return result.trim();
}
function beautifyJS(code){
  // Simple JS beautifier: fix indentation
  var lines=code.split('\n');
  var result=[],indent=0;
  for(var i=0;i<lines.length;i++){
    var line=lines[i].trim();
    if(!line){result.push('');continue;}
    // Dedent for closing braces/brackets
    if(line.startsWith('}')||line.startsWith(']')||line.startsWith(')'))indent=Math.max(0,indent-1);
    result.push('  '.repeat(indent)+line);
    // Indent for opening braces/brackets (at end of line)
    if(line.endsWith('{')||line.endsWith('[')||line.endsWith('('))indent++;
    // Handle one-line braces
    var openC=(line.match(/\{/g)||[]).length;
    var closeC=(line.match(/\}/g)||[]).length;
    indent+=openC-closeC;
    indent=Math.max(0,indent);
  }
  return result.join('\n');
}
function beautifyCSS(code){
  var result='',indent=0;
  var parts=code.split(/([{};])/);
  for(var i=0;i<parts.length;i++){
    var p=parts[i];
    if(!p||p==='')continue;
    if(p==='{'){result+=' {\n';indent++;continue;}
    if(p==='}'){indent=Math.max(0,indent-1);result+='\n'+'  '.repeat(indent)+'}\n';continue;}
    if(p===';'){result+=';\n';continue;}
    result+='  '.repeat(indent)+p.trim()+(p.trim()&&!(parts[i+1]==='{')?' ':'');  }
  return result.replace(/\n{3,}/g,'\n\n').trim();
}

function togglePreviewFullscreen(){
  var pa=document.getElementById('previewArea');
  var btn=document.getElementById('fullscreenBtn');
  var isFs=document.body.classList.toggle('preview-fs');
  btn.innerHTML=isFs?'<i class="fas fa-compress"></i>':'<i class="fas fa-expand"></i>';
  btn.title=isFs?'Свернуть':'На весь экран';
  if(isFs){
    if(!pa.classList.contains('show'))pa.classList.add('show');
    refreshPreview();
  }
}

// === Модальные окна для создания файлов/папок ===
function showFileModal(){
  const existing=document.querySelector('.file-modal-overlay');
  if(existing)existing.remove();
  const overlay=document.createElement('div');
  overlay.className='file-modal-overlay modal-overlay active';
  overlay.onclick=(e)=>{if(e.target===overlay)overlay.remove()};
  overlay.innerHTML='<div class="modal-box"><h3><i class="fas fa-file-circle-plus" style="color:var(--accent)"></i> Новый файл</h3><div style="margin-bottom:12px"><label>Имя файла</label><input id="newFileName" value="new.html" placeholder="index.html, style.css, script.js, main.py"></div><div style="margin-bottom:12px"><label>Расположение</label><select id="newFileParent"></select></div><div class="modal-actions"><button class="btn-primary" onclick="submitNewFile()">Создать</button><button class="btn-cancel" onclick="this.closest(\'.file-modal-overlay\').remove()">Отмена</button></div></div>';
  document.body.appendChild(overlay);
  // Заполняем список папок
  const sel=document.getElementById('newFileParent');
  sel.innerHTML='<option value="/">📁 Корень (/)</option>'+getAllFolders().map(p=>`<option value="${p}">📁 ${p}</option>`).join('');
  setTimeout(()=>{const inp=document.getElementById('newFileName');inp.focus();inp.select()},50);
  document.getElementById('newFileName').onkeydown=(e)=>{if(e.key==='Enter')submitNewFile();if(e.key==='Escape')overlay.remove()};
}

function showFolderModal(){
  const existing=document.querySelector('.file-modal-overlay');
  if(existing)existing.remove();
  const overlay=document.createElement('div');
  overlay.className='file-modal-overlay modal-overlay active';
  overlay.onclick=(e)=>{if(e.target===overlay)overlay.remove()};
  overlay.innerHTML='<div class="modal-box"><h3><i class="fas fa-folder-circle-plus" style="color:var(--green)"></i> Новая папка</h3><div style="margin-bottom:12px"><label>Имя папки</label><input id="newFolderName" value="new_folder" placeholder="css, js, assets..."></div><div style="margin-bottom:12px"><label>Родительская папка</label><select id="newFolderParent"></select></div><div class="modal-actions"><button class="btn-primary" onclick="submitNewFolder()">Создать</button><button class="btn-cancel" onclick="this.closest(\'.file-modal-overlay\').remove()">Отмена</button></div></div>';
  document.body.appendChild(overlay);
  const sel=document.getElementById('newFolderParent');
  sel.innerHTML='<option value="/">📁 Корень (/)</option>'+getAllFolders().map(p=>`<option value="${p}">📁 ${p}</option>`).join('');
  setTimeout(()=>{const inp=document.getElementById('newFolderName');inp.focus();inp.select()},50);
  document.getElementById('newFolderName').onkeydown=(e)=>{if(e.key==='Enter')submitNewFolder();if(e.key==='Escape')overlay.remove()};
}

function getAllFolders(){
  const folders=new Set(['/']);
  function collect(items){
    items.forEach(f=>{
      if(f.type==='folder'){
        folders.add(f.path);
        if(f.children)collect(f.children);
      }
    });
  }
  collect(fileTree);
  return[...folders].filter(p=>p!=='/').sort();
}

async function submitNewFile(){
  const name=document.getElementById('newFileName').value.trim();
  const parent=document.getElementById('newFileParent').value;
  if(!name){alert('Введите имя файла');return}
  try{
    let content='';
    const ext=name.split('.').pop().toLowerCase();
    if(ext==='html')content='<!DOCTYPE html>\n<html lang="ru"><head>\n<meta charset="UTF-8">\n<title>Новая страница</title>\n</head>\n<body>\n\n</body>\n</html>';
    else if(ext==='css')content='/* Стили */\n';
    else if(ext==='js')content='// JavaScript\n';
    else if(ext==='py')content='#!/usr/bin/env python3\n# Python приложение\n\n';
    else if(ext==='json')content='{\n  \n}';
    else content='';

    const fullPath=parent==='/'?name:(parent+'/'+name);
    const r=await fetch('/api/extensions/'+extName+'/files/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:fullPath,content})});
    const d=await r.json();
    if(d.error){alert('Ошибка: '+d.error);return}
    showToast('✓ Файл создан: '+fullPath);
    document.querySelector('.file-modal-overlay')?.remove();
    loadFiles();
  }catch(e){alert('Ошибка: '+e.message)}
}

async function submitNewFolder(){
  const name=document.getElementById('newFolderName').value.trim();
  const parent=document.getElementById('newFolderParent').value;
  if(!name){alert('Введите имя папки');return}
  const fullPath=parent==='/'?name:(parent+'/'+name);
  try{
    await fetch('/api/extensions/'+extName+'/files/create-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:fullPath})});
    showToast('✓ Папка создана: '+fullPath);
    document.querySelector('.file-modal-overlay')?.remove();
    loadFiles();
  }catch(e){alert(e.message)}
}

async function createNewFile(){showFileModal()}
async function createNewFolder(){showFolderModal()}

function loadFiles(){
  fetch('/api/extensions/'+extName+'/files').then(r=>r.json()).then(d=>{
    files=d.files||[];
    fileTree=buildTree(files);
    document.getElementById('extTitle').textContent=extName;
    renderFileTree();
  }).catch(e=>{
    console.error('loadFiles error:',e);
    showToast('Ошибка загрузки файлов');
  });
}

async function deleteFile(path){
  if(!confirm('Удалить '+path+'?'))return;
  try{
    await fetch('/api/extensions/'+extName+'/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
    const idx=openTabs.findIndex(t=>t.path===path);if(idx>=0)closeTab(idx);
    loadFiles();
  }catch(e){alert(e.message)}
}
function deleteCurrentFile(){if(currentFilePath)deleteFile(currentFilePath);}
function showToast(msg,type){type=type||'success';var container=document.getElementById('toastContainer');if(!container){container=document.createElement('div');container.id='toastContainer';container.style.cssText='position:fixed;bottom:30px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none';document.body.appendChild(container);}var t=document.createElement('div');t.className='toast '+type;t.textContent=msg;container.appendChild(t);setTimeout(function(){t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(function(){t.remove()},300)},2000);}

// === RESIZABLE PANELS ===
function initResizers(){
  loadLayout();
  document.querySelectorAll('.resizer').forEach(function(r){
    r.addEventListener('mousedown',function(e){
      e.preventDefault();
      var targetId=r.dataset.target;
      var targetEl=document.getElementById(targetId);
      if(!targetEl)return;
      var dir=parseFloat(r.dataset.dir)||1;
      var startX=e.clientX;
      var startW=targetEl.offsetWidth;
      function onMove(e2){
        var dx=e2.clientX-startX;
        var newW=Math.max(80,Math.min(800,startW+dir*dx));
        targetEl.style.width=newW+'px';
        targetEl.style.flex='0 0 auto';
      }
      function onUp(){
        document.removeEventListener('mousemove',onMove);
        document.removeEventListener('mouseup',onUp);
        r.classList.remove('active');
        saveLayout();
        document.body.style.cursor='';
        document.body.style.userSelect='';
      }
      document.addEventListener('mousemove',onMove);
      document.addEventListener('mouseup',onUp);
      r.classList.add('active');
      document.body.style.cursor='col-resize';
      document.body.style.userSelect='none';
    });
  });
}
function saveLayout(){
  var layout={};
  var panels=['sidebar','previewArea','aiPanel','configPanel'];
  panels.forEach(function(id){
    var el=document.getElementById(id);
    if(el&&el.style.width)layout[id+'Width']=el.style.width;
  });
  try{localStorage.setItem('extEditLayout',JSON.stringify(layout));}catch(e){}
}
function loadLayout(){
  try{
    var data=JSON.parse(localStorage.getItem('extEditLayout'));
    if(!data)return;
    if(data.sidebarWidth)document.getElementById('sidebar').style.width=data.sidebarWidth;
    if(data.previewWidth)document.getElementById('previewArea').style.width=data.previewWidth;
    if(data.aiWidth)document.getElementById('aiPanel').style.width=data.aiWidth;
    if(data.configWidth)document.getElementById('configPanel').style.width=data.configWidth;
  }catch(e){}
}
function updateResizers(){
  document.getElementById('resizerPreview').style.display=document.getElementById('previewArea').classList.contains('show')?'':'none';
  document.getElementById('resizerAi').style.display=document.getElementById('aiPanel').classList.contains('show')?'':'none';
  document.getElementById('resizerConfig').style.display=document.getElementById('configPanel').classList.contains('show')?'':'none';
}

// === Ext Settings ===
const AVAILABLE_HOOKS={
  agent:['before_chat','after_chat','tool_execute','memory_recall','before_message','after_message'],
  books:['book_open','book_search','bookmark_add','page_turn','before_book_render'],
  media:['track_play','video_open','album_view'],
  notes:['note_open','note_save','note_render'],
  search:['before_search','after_search','result_render'],
  ui:['nav_render','theme_change','desktop_render'],
  desktop:['desktop_render','desktop_init','window_open','window_close','icon_click','context_menu','wallpaper_change','taskbar_render','startmenu_render','session_start','session_end'],
  settings:['settings_render','settings_save','section_render'],
  files:['file_open','file_save','file_delete','file_select','before_file_render'],
  home:['home_init','home_render','nav_render','theme_change'],
  genlab:['genlab_init','genlab_render','job_submit','job_complete','generation_done'],
  browser:['browser_init','browser_render','tab_open','tab_close','page_load','download_start'],
  social:['social_init','social_render','feed_render','post_create','message_send'],
  qdrant:['qdrant_init','qdrant_render','index_build','index_rebuild','search_query'],
  ha_panel:['ha_init','ha_render','state_change','device_toggle','scene_activate'],
  docker:['docker_init','docker_render','container_start','container_stop'],
  ai_engine:['ai_engine_render','engine_start','engine_stop','model_change'],
  network:['network_render','node_join','node_leave'],
  all:['load','init','render']
};

function renderHooksCheckboxes(selectedHooks){
  const container=document.getElementById('hooksContainer');
  const module=document.getElementById('settingsTargetModule').value;
  const hooks=AVAILABLE_HOOKS[module]||AVAILABLE_HOOKS['all']||[];
  container.innerHTML=hooks.map(h=>'<label style="display:inline-flex;align-items:center;gap:3px;padding:3px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:11px"><input type="checkbox" class="hook-cb" value="'+h+'" '+(selectedHooks.includes(h)?'checked':'')+'> '+h+'</label>').join('');
}

function renderPermissionsCheckboxes(selectedPerms){
  const container=document.getElementById('permissionsContainer');
  container.innerHTML=allPermissions.map(p=>'<label style="display:inline-flex;align-items:center;gap:3px;padding:3px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:11px" title="'+(p.description||'')+'"><input type="checkbox" class="perm-cb" value="'+p.id+'" '+(selectedPerms.includes(p.id)?'checked':'')+'> '+p.id+'</label>').join('');
}

function openExtSettings(){
  document.getElementById('settingsName').value=extMeta.name||extName;
  document.getElementById('settingsVersion').value=extMeta.version||'1.0.0';
  document.getElementById('settingsDesc').value=extMeta.description||'';
  document.getElementById('settingsAuthor').value=extMeta.author||'';
  document.getElementById('settingsIcon').value=extMeta.icon||'fa-puzzle-piece';
  document.getElementById('settingsTargetModule').value=extMeta.target_module||'all';
  document.getElementById('settingsEntryPoint').value=extMeta.entry_point||'main.py';
  document.getElementById('settingsFrontendEntry').value=extMeta.frontend_entry||'frontend.js';
  document.getElementById('settingsGitUrl').value=extMeta.git_url||'';
  document.getElementById('settingsSandbox').value=extMeta.sandbox?'true':'false';
  renderHooksCheckboxes(extMeta.hooks||[]);
  renderPermissionsCheckboxes(extMeta.permissions||['storage']);
  document.getElementById('appSettingsModal').style.display='flex';
}
function closeExtSettings(){document.getElementById('extSettingsModal').style.display='none'}
document.getElementById('settingsTargetModule')?.addEventListener('change',function(){
  renderHooksCheckboxes(
    Array.from(document.querySelectorAll('.hook-cb:checked')).map(cb=>cb.value)
  );
});
async function saveExtSettings(){
  const meta={
    name:document.getElementById('settingsName').value,
    version:document.getElementById('settingsVersion').value,
    description:document.getElementById('settingsDesc').value,
    author:document.getElementById('settingsAuthor').value,
    icon:document.getElementById('settingsIcon').value,
    target_module:document.getElementById('settingsTargetModule').value,
    entry_point:document.getElementById('settingsEntryPoint').value,
    frontend_entry:document.getElementById('settingsFrontendEntry').value,
    git_url:document.getElementById('settingsGitUrl').value,
    sandbox:document.getElementById('settingsSandbox').value==='true',
    hooks:Array.from(document.querySelectorAll('.hook-cb:checked')).map(cb=>cb.value),
    permissions:Array.from(document.querySelectorAll('.perm-cb:checked')).map(cb=>cb.value),
  };
  try{
    const r=await fetch('/api/extensions/'+extName+'/metadata',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({metadata:meta})});
    const d=await r.json();
    if(d.ok){
      Object.assign(extMeta,meta);
      document.getElementById('extTitle').textContent=meta.name;
      closeExtSettings();
      showToast('✓ Настройки сохранены');
      saveExtPermissions(meta.permissions);
    }else{showToast('Ошибка: '+(d.error||''))}
  }catch(e){showToast('Ошибка: '+e.message)}
}
async function saveExtPermissions(perms){
  try{
    await fetch('/api/extensions/'+extName+'/permissions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({permissions:perms})});
  }catch(e){}
}

// === AI CHAT ===
let aiModels=[];
let aiCurrentModel='';
function toggleAiChat(){
  document.getElementById('aiPanel').classList.toggle('show');
  updateResizers();
  if(!aiModels.length)loadAiModels();
}

async function loadAiModels(){
  try{
    const r=await fetch('/api/models');
    const data=await r.json();
    aiModels=data.models||[];
    aiCurrentModel=data.current||'';
    const sel=document.getElementById('aiModelSelect');
    if(sel&&aiModels.length){
      sel.innerHTML=aiModels.map(m=>`<option value="${esc(m)}" ${m===aiCurrentModel?'selected':''}>${esc(m)}</option>`).join('');
    }
  }catch(e){console.error('Failed to load models:',e)}
}

async function aiSend(){
  const input=document.getElementById('aiInput');const q=input.value.trim();if(!q)return;input.value='';
  const msgs=document.getElementById('aiMessages');
  const sendBtn=document.querySelector('.ai-send-btn');
  msgs.innerHTML+=`<div class="ai-msg-user">${esc(q)}</div>`;
  msgs.scrollTop=msgs.scrollHeight;
  const thinkingId='aiThink_'+Date.now();
  msgs.innerHTML+=`<div id="${thinkingId}" class="ai-thinking"><div class="spinner"></div> AI думает...</div>`;
  msgs.scrollTop=msgs.scrollHeight;
  if(sendBtn){sendBtn.disabled=true;sendBtn.style.opacity='0.5';sendBtn.style.cursor='default';}
  let code='';let lang='html';
  if(useMonaco&&monacoInstance)code=monacoInstance.getValue();else if(codeMirror)code=codeMirror.getValue();
  const tab=openTabs[activeTab];
  if(tab){const ext=tab.name.split('.').pop().toLowerCase();const lm={js:'javascript',py:'python',json:'json',css:'css',html:'html',htm:'html',md:'markdown',xml:'xml',yml:'yaml',yaml:'yaml',sh:'bash',bat:'batch',txt:'text'};lang=lm[ext]||'html';}
  const selectedModel=document.getElementById('aiModelSelect')?.value||aiCurrentModel;
  const systemPrompt='Ты ассистент-разработчик в редакторе кода. Пользователь редактирует файл "'+(tab?tab.name:'unknown')+'" на языке '+lang+'.\\n\\nПолный код файла:\\n```'+lang+'\\n'+code+'\\n```\\n\\nПравила:\\n1. Проанализируй ВЕСЬ код от начала до конца, пойми его полную структуру\\n2. Если пользователь просит найти конкретную строку/фрагмент - ищи её во всём коде, не только в начале. Сканируй каждую строку\\n3. Если нужен контекст из другого файла - напиши [FILE: путь/к/файлу.расширение]\\n4. Изменения выдавай полным блоком кода с ```'+lang+'\\n5. Показывай что изменилось (комментарий // ИЗМЕНЕНО: ...)\\n6. Отвечай на русском\\n7. Если просит несколько изменений - делай последовательно';
  try{
    const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,system_prompt:systemPrompt,model:selectedModel||undefined})});
    const d=await r.json();const answer=d.answer||'Нет ответа';aiLastResponse=answer;
    const thinkEl=document.getElementById(thinkingId);if(thinkEl)thinkEl.remove();
    msgs.innerHTML+=`<div class="ai-msg-assistant">${formatAiResponse(answer)}</div>`;
    msgs.scrollTop=msgs.scrollHeight;
    if(document.getElementById('autoApplyToggle').checked)setTimeout(aiApplyCode,300);
  }catch(e){
    const thinkEl=document.getElementById(thinkingId);if(thinkEl)thinkEl.remove();
    msgs.innerHTML+=`<div style="background:rgba(239,68,68,0.1);padding:8px 12px;border-radius:12px;color:var(--red);font-size:13px">Ошибка: ${e.message}</div>`;
  }finally{
    if(sendBtn){sendBtn.disabled=false;sendBtn.style.opacity='';sendBtn.style.cursor='';}
  }
}
function extractCodeBlocks(text){
  var patterns=[
    /```(?:\w+)?[ \t]*\r?\n+([\s\S]*?)```/,
    /```(?:\w+)?\s*\n([\s\S]*?)```/,
    /```([\s\S]*?)```/,
    /`([^`]+)`/
  ];
  for(var i=0;i<patterns.length;i++){
    var m=text.match(patterns[i]);
    if(m&&m[1]&&m[1].trim())return m[1].trim();
  }
  return null;
}
function aiApplyCode(){
  var code=extractCodeBlocks(aiLastResponse);
  if(code){
    if(useMonaco&&monacoInstance)monacoInstance.setValue(code);
    else if(codeMirror)codeMirror.setValue(code);
    showToast('✓ Применён');
  }else showToast('Код не найден');
}
function aiInsertCode(){
  var codeToAdd=extractCodeBlocks(aiLastResponse);
  if(codeToAdd){
    if(useMonaco&&monacoInstance){
      var sel=monacoInstance.getSelection();
      if(sel.isEmpty()){
        var pos=sel.getStartPosition();
        monacoInstance.executeEdits('ai',[{range:new monaco.Range(pos.lineNumber,pos.column,pos.lineNumber,pos.column),text:codeToAdd,forceMoveMarkers:true}]);
      }else{
        monacoInstance.executeEdits('ai',[{range:sel,text:codeToAdd,forceMoveMarkers:true}]);
      }
    }else if(codeMirror){
      var from=codeMirror.getCursor('start');
      var to=codeMirror.getCursor('end');
      codeMirror.replaceRange(codeToAdd,from,to);
    }
    showToast('✓ Вставлен');
  }else showToast('Код не найден');
}
function formatAiResponse(t){
  let idx=0;
  let fileRefs=[];
  t=t.replace(/\[FILE\s*:\s*([^\]]+)\]/g,function(m,path){
    var id='__FILEB_'+(fileRefs.length)+'__';
    fileRefs.push({id:id,path:path.trim()});
    return id;
  });
  var html=esc(t)
    .replace(/```(\w*)[ \t]*\r?\n+([\s\S]*?)```/g,(full,lang,code)=>{
      const blockId='codeBlock_'+(idx++);
      return '<div class="code-block-wrapper"><div class="code-block-header"><button onclick="copyCodeBlock(\''+blockId+'\')"><i class="fas fa-copy"></i></button><button onclick="pasteCodeBlock(\''+blockId+'\')"><i class="fas fa-paste"></i></button></div><pre id="'+blockId+'" class="code-block-content"><code>'+esc(code.trim())+'</code></pre></div>';
    })
    .replace(/`([^`]+)`/g,'<code style="background:var(--bg3);padding:1px 4px;border-radius:3px;font-size:11px">$1</code>')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\n/g,'<br>');
  fileRefs.forEach(function(b){
    var safePath=esc(b.path);
    html=html.replace(b.id,'<br><button onclick="openAiFile(\''+safePath+'\')" style="padding:4px 10px;background:var(--accent);border:none;border-radius:6px;color:#fff;cursor:pointer;font-size:11px;margin:4px 0"><i class="fas fa-file"></i> \uD83D\uDCC2 '+safePath+'</button>');
  });
  return html;
}

function copyCodeBlock(id){
  const el=document.getElementById(id);
  if(!el)return;
  const code=el.querySelector('code')?.textContent||el.textContent;
  navigator.clipboard.writeText(code).then(()=>showToast('✓ Скопировано')).catch(()=>showToast('Ошибка копирования'));
}

function pasteCodeBlock(id){
  const el=document.getElementById(id);
  if(!el)return;
  const code=el.querySelector('code')?.textContent||el.textContent;
  if(useMonaco&&monacoInstance){
    const sel=monacoInstance.getSelection();
    if(sel.isEmpty()){
      const pos=sel.getStartPosition();
      monacoInstance.executeEdits('paste',[{range:new monaco.Range(pos.lineNumber,pos.column,pos.lineNumber,pos.column),text:code,forceMoveMarkers:true}]);
    }else{
      monacoInstance.executeEdits('paste',[{range:sel,text:code,forceMoveMarkers:true}]);
    }
  }else if(codeMirror){
    const from=codeMirror.getCursor('start');
    const to=codeMirror.getCursor('end');
    codeMirror.replaceRange(code,from,to);
  }
  showToast('✓ Вставлено в редактор');
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function openAiFile(path){
  openFile(path);
  showToast('\uD83D\uDCC2 '+path);
}

// ============ CONSOLE ============
function initConsoleListener(){
  window.addEventListener('message',function(e){
    if(e.data&&e.data.source==='appPreviewConsole'){
      var level=e.data.level||'log';
      var args=e.data.args||[];
      var time=e.data.time||'';
      args.forEach(function(a){addConsoleEntry(level,a,time);});
    }else if(e.data&&e.data.source==='appPreviewInspect'){
      addInspectorEntry(e.data.info);
    }else if(e.data&&e.data.source==='visEditorStyle'){
      showStyleModal(e.data.action||'edit',e.data.current||{});
    }else if(e.data&&e.data.source==='visEditorSync'){
      if(activeTab>=0&&openTabs[activeTab]){
        openTabs[activeTab].content=e.data.html;
        if(useMonaco&&monacoInstance)monacoInstance.setValue(e.data.html);
        else if(codeMirror)codeMirror.setValue(e.data.html);
      }
    }else if(e.data&&e.data.source==='visEditorToast'){
      showToast(e.data.msg||'');
    }else if(e.data&&e.data.source==='visEditorImgLib'&&e.data.action==='open'){
      showImgLibDialog();
    }else if(e.data&&e.data.source==='visEditorImgLib'&&e.data.action==='insert'){
      var frame=document.getElementById('previewFrame');
      if(frame&&frame.contentWindow){
        frame.contentWindow.postMessage({source:'visEditorResult',status:'image',url:e.data.url,alt:e.data.alt},'*');
      }
    }
  });
}

function showImgLibDialog(){
  var existing=document.querySelector('.img-lib-overlay');
  if(existing)existing.remove();
  var overlay=document.createElement('div');
  overlay.className='file-modal-overlay modal-overlay active';
  overlay.style.zIndex='10000';
  overlay.onclick=function(e){if(e.target===overlay)overlay.remove();};
  // Collect image files from the file tree
  var images=[];
  function collect(items){
    if(!items)return;
    items.forEach(function(f){
      if(f.type==='file'&&f.name&&f.name.match(/\.(png|jpg|jpeg|gif|svg|webp|ico)$/i)){
        images.push(f);
      }
      if(f.children)collect(f.children);
    });
  }
  collect(fileTree);
  var html='<div class="modal-box" style="max-width:520px"><h3><i class="fas fa-images"></i> Изображения</h3>';
  html+='<div style="margin-bottom:10px"><button class="btn-primary" onclick="uploadImgLib()" style="padding:7px 14px;border-radius:8px;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;cursor:pointer;font-size:12px;font-weight:600"><i class="fas fa-upload"></i> Загрузить</button></div>';
  if(images.length===0){
    html+='<p style="color:var(--text3);font-size:13px">Нет изображений. Загрузите через кнопку выше.</p>';
  }else{
    html+='<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-height:400px;overflow-y:auto">';
    images.forEach(function(img){
      var url='/api/extensions/'+extName+'/files/read?path='+encodeURIComponent(img.path);
      html+='<div class="img-lib-item" data-url="'+url.replace(/"/g,'&quot;')+'" data-path="'+img.path+'" style="border:1px solid var(--border);border-radius:8px;overflow:hidden;cursor:pointer;transition:var(--tr);background:var(--bg)" onclick="selectImgLib(this,\''+url.replace(/\\/g,'\\\\').replace(/'/g,"\\'")+'\',\''+img.name.replace(/'/g,"\\'")+'\')">';
      html+='<div style="height:80px;background:var(--bg3);display:flex;align-items:center;justify-content:center;overflow:hidden">';
      if(img.name.match(/\.svg$/i)){
        html+='<object data="'+url+'" type="image/svg+xml" style="max-width:100%;max-height:100%"></object>';
      }else{
        html+='<img src="'+url+'" style="max-width:100%;max-height:100%;object-fit:contain" onerror="this.style.display=\'none\'">';
      }
      html+='</div><div style="padding:4px 6px;font-size:10px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+img.name+'</div>';
      html+='</div>';
    });
    html+='</div>';
  }
  html+='<div class="modal-actions"><button class="btn-cancel" onclick="this.closest(\'.file-modal-overlay\').remove()">Закрыть</button></div></div>';
  overlay.innerHTML=html;
  document.body.appendChild(overlay);
}
function selectImgLib(el,url,name){
  var frame=document.getElementById('previewFrame');
  if(frame&&frame.contentWindow){
    frame.contentWindow.postMessage({source:'visEditorResult',status:'image',url:url,alt:name},'*');
  }
  el.closest('.file-modal-overlay').remove();
  showToast('✓ Изображение вставлено: '+name);
}
async function uploadImgLib(){
  var ov=document.querySelector('.img-lib-overlay');
  var input=document.createElement('input');
  input.type='file';
  input.accept='image/png,image/jpeg,image/gif,image/svg+xml,image/webp';
  input.multiple=true;
  input.onchange=async function(){
    var files=input.files;
    if(!files||!files.length)return;
    showToast('⏳ Загрузка '+files.length+' файлов...');
    for(var i=0;i<files.length;i++){
      try{
        var fi=i;
        var fr=new FileReader();
        fr.onload=async function(){
          var base64=fr.result.split(',')[1];
          var fileName=files[fi].name;
          var r=await fetch('/api/extensions/'+extName+'/files/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:'images/'+fileName,content:base64})});
          var d=await r.json();
          if(d.error)showToast('Ошибка: '+d.error,'error');
        };
        fr.readAsDataURL(files[i]);
      }catch(e){showToast('Ошибка загрузки: '+e.message,'error');}
    }
    showToast('✓ Загружено');
    loadFiles();
    if(ov)ov.remove();
    showImgLibDialog();
  };
  input.click();
}

function getConsoleCaptureScript(){
  return '(function(){'+
    'var levels=["log","info","warn","error"];'+
    'levels.forEach(function(lvl){'+
      'var orig=console[lvl];'+
      'console[lvl]=function(){'+
        'try{orig.apply(console,arguments)}catch(e){}'+
        'var txt=Array.prototype.slice.call(arguments).map(function(a){'+
          'try{return typeof a==="object"?JSON.stringify(a,null,2):String(a)}catch(e){return String(a)}'+
        '}).join(" ");'+
        'window.parent.postMessage({source:"appPreviewConsole",level:lvl,args:[txt],time:new Date().toLocaleTimeString()},"*");'+
      '};'+
    '});'+
    'window.addEventListener("error",function(e){'+
      'window.parent.postMessage({source:"appPreviewConsole",level:"error",args:["Ошибка: "+e.message+" ("+e.filename+":"+e.lineno+":"+e.colno+")"],time:new Date().toLocaleTimeString()},"*");'+
    '});'+
    'window.addEventListener("unhandledrejection",function(e){'+
      'window.parent.postMessage({source:"appPreviewConsole",level:"error",args:["Unhandled: "+e.reason],time:new Date().toLocaleTimeString()},"*");'+
    '});'+
  '})();';
}

function addConsoleEntry(level,text,time){
  var panel=document.getElementById('consolePanel');
  if(!panel)return;
  var empty=panel.querySelector('.console-empty');
  if(empty)empty.remove();
  var div=document.createElement('div');
  div.className='console-entry '+level;
  var ts=time||new Date().toLocaleTimeString();
  div.innerHTML='<span class="time">'+ts+'</span><span class="lvl">'+level+'</span><span class="msg">'+esc(text)+'</span>';
  panel.appendChild(div);
  panel.scrollTop=panel.scrollHeight;
  consoleEntries++;
  document.getElementById('consoleClearBtn').style.display='inline-block';
}

function clearConsole(){
  var panel=document.getElementById('consolePanel');
  panel.innerHTML='<div style="padding:12px;color:var(--text3);font-size:12px;text-align:center">Консоль очищена</div>';
  consoleEntries=0;
  document.getElementById('consoleClearBtn').style.display='none';
}

function switchPanelTab(tab){
  document.querySelectorAll('.panel-tab').forEach(function(t){t.classList.toggle('active',t.dataset.panel===tab);});
  document.getElementById('terminal').style.display=tab==='term'?'flex':'none';
  document.getElementById('consolePanel').className='console-panel'+(tab==='console'?' show':'');
  if(tab==='term'&&terminalFitAddon)setTimeout(function(){terminalFitAddon.fit()},50);
}

</script>
</body></html>"""
