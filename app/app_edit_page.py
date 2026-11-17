#!/usr/bin/env python3
"""App Edit Page — файловый менеджер + редактор кода + терминал + сервер."""

PAGE_APP_EDIT = r"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Редактор приложения</title>
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

/* Config Panel */
.config-panel{width:350px;background:var(--glass);backdrop-filter:blur(12px);border-left:1px solid var(--glass-border);display:none;flex-direction:column;flex-shrink:0;overflow:hidden}
.config-panel.show{display:flex}
.config-header{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}
.config-header .cfg-icon{color:var(--orange)}
.config-header button{background:linear-gradient(135deg,var(--green),#059669);border:none;border-radius:var(--radius-sm);color:#fff;cursor:pointer;font-size:11px;padding:5px 12px;transition:var(--tr);margin-left:auto}
.config-header button:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(16,185,129,0.3)}
.config-content{flex:1;overflow-y:auto;padding:12px;font-size:12px}
.config-section{margin-bottom:16px}
.config-section-title{font-size:11px;font-weight:700;color:var(--text2);text-transform:uppercase;margin-bottom:8px;letter-spacing:.5px}
.config-field{margin-bottom:10px}
.config-field label{display:block;font-size:11px;color:var(--text2);margin-bottom:4px;font-weight:600}
.config-field input,.config-field select,.config-field textarea{width:100%;padding:7px 10px;background:var(--bg);border:1.5px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:12px;outline:none;font-family:inherit;transition:var(--tr)}
.config-field input:focus,.config-field select:focus,.config-field textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-light)}
.config-array-item{display:flex;gap:4px;margin-bottom:4px;align-items:center}
.config-array-item input{flex:1;min-width:0}
.config-array-item .rm-btn{padding:3px 8px;background:rgba(239,68,68,0.15);border:1px solid transparent;border-radius:4px;color:var(--red);cursor:pointer;font-size:10px;transition:var(--tr)}
.config-array-item .rm-btn:hover{background:var(--red);color:#fff}
.config-add-btn{padding:5px;background:var(--bg3);border:1px dashed var(--border);border-radius:var(--radius-sm);color:var(--text2);cursor:pointer;font-size:11px;width:100%;text-align:center;margin-top:4px;transition:var(--tr)}
.config-add-btn:hover{background:var(--accent-light);color:var(--accent);border-color:var(--accent)}
.config-row{display:flex;gap:8px}
.config-row .config-field{flex:1}
.config-status{padding:6px 10px;font-size:11px;border-radius:var(--radius-sm);margin-bottom:8px;display:none;animation:fadeIn .2s}
.config-status.ok{display:block;background:rgba(16,185,129,0.1);color:var(--green);border:1px solid rgba(16,185,129,0.2)}
.config-status.err{display:block;background:rgba(239,68,68,0.1);color:var(--red);border:1px solid rgba(239,68,68,0.2)}

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

/* ===== Визуальный редактор (панель в хост-странице) =====
   Оформление только через переменные темы страницы (--bg/--accent/--text),
   поэтому панель следует и светлой теме (body.light), и палитре сайта. */
.ve-panel{width:330px;background:var(--glass);backdrop-filter:blur(16px) saturate(180%);-webkit-backdrop-filter:blur(16px) saturate(180%);border-left:1px solid var(--glass-border);display:none;flex-direction:column;flex-shrink:0}
.ve-panel.show{display:flex}
.ve-panel-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px;font-weight:700;color:var(--text)}
.ve-panel-head .ve-ico{color:var(--accent);font-size:14px}
.ve-panel-head .ve-close{margin-left:auto;background:none;border:none;color:var(--text2);cursor:pointer;font-size:15px;padding:2px 6px;border-radius:6px;transition:var(--tr)}
.ve-panel-head .ve-close:hover{background:var(--hover);color:var(--text)}
.ve-mode{display:flex;gap:6px;padding:8px 12px;border-bottom:1px solid var(--border)}
.ve-mode button{flex:1;padding:6px 8px;font-size:11px;font-weight:600;border-radius:8px;border:1px solid var(--border);background:var(--bg3);color:var(--text2);cursor:pointer;transition:var(--tr)}
.ve-mode button:hover{border-color:var(--accent);color:var(--text)}
.ve-mode button.active{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#fff}
.ve-crumb{padding:7px 12px;font-size:10px;font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--text3);border-bottom:1px solid var(--border);white-space:nowrap;overflow:auto}
.ve-body{flex:1;overflow-y:auto;padding:6px 12px 12px}
.ve-sec{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin:14px 0 6px;border-top:1px solid var(--border);padding-top:10px}
.ve-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.ve-field{display:block;margin-bottom:6px}
.ve-field>span{display:block;font-size:10px;color:var(--text3);margin-bottom:3px}
.ve-field input,.ve-field select,.ve-field textarea{width:100%;padding:5px 7px;background:var(--bg);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:12px;font-family:inherit;outline:none;transition:var(--tr)}
.ve-field input:focus,.ve-field select:focus,.ve-field textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
.ve-field input[type=color]{height:28px;padding:2px}
.ve-field textarea{resize:vertical;min-height:52px}
.ve-foot{padding:10px 12px;border-top:1px solid var(--border);display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.ve-btn{padding:7px 11px;border-radius:9px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:var(--tr)}
.ve-btn:hover{border-color:var(--accent);color:var(--accent)}
.ve-btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#fff}
.ve-btn.primary:hover{filter:brightness(1.1);color:#fff}
.ve-btn.danger{color:var(--red);border-color:rgba(239,68,68,.35)}
.ve-btn.danger:hover{border-color:var(--red);color:var(--red)}
.ve-hint{font-size:11px;color:var(--text3);padding:10px 12px;line-height:1.5}

</style>
<script src="/static/js/i18n.js"></script>
</head><body>
<script type="application/json" id="appIdJson">{{app_id_json}}</script>
<div class="toolbar" id="toolbar">
  <a href="/apps" class="toolbar-btn" title="Назад"><i class="fas fa-arrow-left"></i></a>
  <span class="toolbar-title" id="appTitle">Редактор</span>
  <span class="status-pill stopped" id="serverStatus" onclick="toggleServer()"><i class="fas fa-circle"></i> Сервер</span>
  <span class="toolbar-sep"></span>
  <button class="toolbar-btn" onclick="openAppSettings()" title="Настройки"><i class="fas fa-cog"></i></button>
  <button class="toolbar-btn" onclick="toggleConfigPanel()" title="Конфиг"><i class="fas fa-sliders-h"></i></button>
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
      <div class="console-panel" id="consolePanel"><div class="console-empty" style="padding:12px;color:var(--text3);font-size:12px;text-align:center">Консоль приложения. Откройте Превью чтобы увидеть логи.</div></div>
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
  <div class="resizer" id="resizerVe" data-target="vePanel" data-dir="-1" style="display:none"></div>
  <div class="ve-panel" id="vePanel">
    <div class="ve-panel-head">
      <i class="fas fa-paint-brush ve-ico"></i>
      <span>Визуальный редактор</span>
      <button class="ve-close" onclick="toggleVisualEditor()" title="Закрыть"><i class="fas fa-times"></i></button>
    </div>
    <div class="ve-mode">
      <button id="veModeSelect" class="active" onclick="veSetMode('select')" title="Выбирать элементы кликом"><i class="fas fa-cursor-pointer"></i> Выбор</button>
      <button id="veModeInteract" onclick="veSetMode('interact')" title="Приложение работает как обычно"><i class="fas fa-hand-pointer"></i> Взаимодействие</button>
    </div>
    <div class="ve-crumb" id="veCrumb">Элемент не выбран</div>
    <div class="ve-body" id="veBody"></div>
    <div class="ve-foot">
      <button class="ve-btn primary" onclick="veSave()"><i class="fas fa-save"></i> Сохранить</button>
      <button class="ve-btn" onclick="veUndo()" title="Отменить (Ctrl+Z)">&#8630;</button>
      <button class="ve-btn" onclick="veRedo()" title="Повторить">&#8631;</button>
      <button class="ve-btn" onclick="veAddElement()"><i class="fas fa-plus"></i> Элемент</button>
      <button class="ve-btn danger" onclick="veDeleteElement()" title="Удалить элемент"><i class="fas fa-trash"></i></button>
    </div>
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
  <div class="resizer" id="resizerConfig" data-target="configPanel" data-dir="-1" style="display:none"></div>
  <!-- Config Panel -->
  <div class="config-panel" id="configPanel">
    <div class="config-header">
      <i class="fas fa-sliders-h" style="color:var(--orange)"></i> Конфигурация приложения
      <button onclick="saveAppConfig()"><i class="fas fa-save"></i> Сохранить</button>
    </div>
    <div class="config-content" id="configContent">
      <div id="configStatus" class="config-status"></div>
      <div id="configForm"></div>
    </div>
  </div>
</div>

<!-- App Settings Modal -->
<div id="appSettingsModal" class="modal-overlay" style="display:none">
  <div class="modal-box">
    <h3><i class="fas fa-cog"></i> Настройки приложения</h3>
    <div style="margin-bottom:12px"><label>Название</label><input id="settingsName"></div>
    <div style="margin-bottom:12px"><label>Тип</label><select id="settingsType"><option value="html">HTML</option><option value="python">Python</option><option value="link">Ссылка</option><option value="background">Фоновое</option></select></div>
    <div style="margin-bottom:12px"><label>Описание</label><textarea id="settingsDesc" rows="3" style="resize:vertical"></textarea></div>
    <div style="margin-bottom:12px"><label>Иконка (emoji или URL)</label><input id="settingsIcon" placeholder="📱 или https://..."></div>
    <div style="margin-bottom:12px"><label>Обложка (URL картинки)</label><input id="settingsCover" placeholder="https://example.com/cover.jpg"></div>
    <div style="margin-bottom:12px"><label>Главный файл</label><input id="settingsMain" placeholder="index.html или main.py"></div>
    <div style="margin-bottom:12px"><label>URL (для ссылок)</label><input id="settingsUrl" placeholder="https://example.com"></div>
    <div class="modal-actions">
      <button class="btn-primary" onclick="saveAppSettings()">💾 Сохранить</button>
      <button class="btn-cancel" onclick="closeAppSettings()">Отмена</button>
    </div>
  </div>
</div>


<script>
let appId=JSON.parse(document.getElementById('appIdJson').textContent);
let appDir='';
let appManifest={};
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
let appConfig={staticDirectories:[],staticFiles:[],routes:[],cors:{enabled:false,origins:'*'}};
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
    const r=await fetch('/api/apps/terminal/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId,text:cmd})});
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
// The editor page used to keep its own 'light' class and its own palette,
// while theme.js (which is connected to all pages) manages the global theme
// on <body>. As a result, the page ignored the site theme, and the visual
// editor panel looked foreign. Now the editor uses the global theme
// (theme.js + design.css), and only falls back to the old logic if
// theme.js is not connected.
function veCurrentTheme(){
  try{
    if(window.InfinityTheme&&InfinityTheme.get)return InfinityTheme.get();
  }catch(e){}
  var c=document.body.className||'';
  if(/\bdark\b/.test(c))return 'dark';
  if(/\blight\b/.test(c))return 'light';
  return 'light';
}
function setEditorTheme(){
  var t=veCurrentTheme();
  var isLight=(t!=='dark');
  if(useMonaco&&monacoInstance&&window.monaco){
    monaco.editor.setTheme(isLight?'vs':'vs-dark');
  }else if(codeMirror){
    codeMirror.setOption('theme',isLight?'default':'material-darker');
  }
  var btn=document.getElementById('themeBtn');
  if(btn)btn.innerHTML=isLight?'<i class="fas fa-sun"></i>':'<i class="fas fa-moon"></i>';
}
function toggleTheme(){
  try{
    if(window.InfinityTheme&&InfinityTheme.toggle){
      InfinityTheme.toggle();
      setEditorTheme();
      return;
    }
  }catch(e){}
  var isLight=document.body.classList.toggle('light');
  localStorage.setItem('appEditTheme',isLight?'light':'dark');
  setEditorTheme();
}
function applyTheme(){
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
window.addEventListener('DOMContentLoaded',function(){initEditor();initTerminal();loadAppInfo();loadFiles();checkServerStatus();initConsoleListener();initResizers();updateResizers();applyTheme();initDragDrop();addKeyBindings();initColorPicker();});

async function loadAppInfo(){
  try{
    const r=await fetch('/api/apps/files?id='+appId);
    const d=await r.json();
    files=d.files||[];appDir=d.dir||'';appManifest=d.manifest||{};
    document.getElementById('appTitle').textContent=d.name||appId;
    renderFileTree();
  }catch(e){console.error(e)}
}

async function checkServerStatus(){
  try{
    const r=await fetch('/api/apps/server/status?id='+appId);
    const d=await r.json();
    const el=document.getElementById('serverStatus');
    if(d.running){el.className='server-status running';el.innerHTML='<i class="fas fa-circle"></i> Сервер: порт '+d.port;}
    else{el.className='server-status stopped';el.innerHTML='<i class="fas fa-circle"></i> Сервер: выкл';}
  }catch(e){
    const el=document.getElementById('serverStatus');
    if(el){el.className='server-status stopped';el.innerHTML='<i class="fas fa-circle"></i> Сервер: ?';}
  }
}

async function toggleServer(){
  try{
    const r=await fetch('/api/apps/server/status?id='+appId);
    const d=await r.json();
    if(d.running){await fetch('/api/apps/server/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId})});}
    else{await fetch('/api/apps/server/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId})});}
    checkServerStatus();
  }catch(e){alert(e.message)}
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
  // Сохраняем текущий файл перед открытием нового
  if(activeTab>=0&&openTabs[activeTab]){
    if(useMonaco&&monacoInstance)openTabs[activeTab].content=monacoInstance.getValue();
    else if(codeMirror)openTabs[activeTab].content=codeMirror.getValue();
  }
  currentFilePath=path;renderFileTree();
  const idx=openTabs.findIndex(t=>t.path===path);
  if(idx>=0){switchTab(idx);return}
  try{
    const r=await fetch('/api/apps/file/get?id='+appId+'&path='+encodeURIComponent(path));
    const d=await r.json();
    if(d.error){showToast('Ошибка: '+d.error);return}
    const name=path.split('/').pop();
    const content=d.content!==undefined?d.content:'';
    openTabs.push({path,content:content,name});
    // Ждём готовности редактора
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
    const r=await fetch('/api/apps/file/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId,path:currentFilePath,content})});
    if(!r.ok){const d=await r.json();showToast('Ошибка: '+(d.error||r.statusText));return}
    showToast('✓ Сохранено');
  }catch(e){showToast('Ошибка: '+e.message)}
}

function togglePreview(){const a=document.getElementById('previewArea');a.classList.toggle('show');if(a.classList.contains('show'))refreshPreview();updateResizers();}
function previewBaseUrl(tab){
  var p=(tab&&tab.path)||'';
  var dir=p.indexOf('/')>=0?p.slice(0,p.lastIndexOf('/')+1):'';
  var enc=dir.split('/').filter(Boolean).map(function(x){return encodeURIComponent(x)}).join('/');
  return '/app-files/'+encodeURIComponent(appId)+'/'+(enc?enc+'/':'');
}
function getVisualEditorScript(){
  var t=document.getElementById('ve-editor-script');
  return t?t.textContent:'';
}
function refreshPreview(){
  if(activeTab<0)return;
  const tab=openTabs[activeTab];
  const ext=tab.name.split('.').pop().toLowerCase();
  // Панель переехала в хост-страницу, а в iframe остался тонкий агент.
  // подсветка, выбор элемента, хендлы и правка текста.
  const src=(visualEditorActive&&veState.marked)?veState.marked:tab.content;
  var sc='<script data-vis="1">', ec='</'+'script>';
  var extraScripts=sc+getConsoleCaptureScript()+ec;
  if(inspectorActive)extraScripts+=sc+getInspectorScript()+ec;
  if(visualEditorActive)extraScripts+=sc+getVisualEditorScript()+ec;
  var baseTag='<base data-vis="1" href="'+previewBaseUrl(tab).replace(/"/g,'&quot;')+'">';
  var inject=baseTag+extraScripts;
  function withBase(html){
    if(/<head[^>]*>/i.test(html))return html.replace(/<head[^>]*>/i,function(m){return m+inject});
    if(/<html[^>]*>/i.test(html))return html.replace(/<html[^>]*>/i,function(m){return m+'<head>'+inject+'</head>'});
    return '<!DOCTYPE html><html><head>'+inject+'</head><body>'+html+'</body></html>';
  }
  if(ext==='html'||ext==='htm'){
    document.getElementById('previewFrame').srcdoc=withBase(src);
  }else if(ext==='css'){
    document.getElementById('previewFrame').srcdoc='<html><head><style>'+src+'</style>'+inject+'</head><body><h1>CSS превью</h1><p>Текст для проверки стилей</p></body></html>';
  }else if(ext==='js'){
    document.getElementById('previewFrame').srcdoc='<html><head>'+inject+'</head><body><script>'+src+'<\/script></body></html>';
  }else if(ext==='py'){
    document.getElementById('previewFrame').srcdoc='<html><head>'+inject+'</head><body style="padding:20px;font-family:monospace"><h3>Python файл</h3><p>Для запуска используйте терминал или кнопку "Сервер"</p><pre style="background:#f5f5f5;padding:12px;border-radius:8px;overflow:auto;max-height:400px">'+esc(src)+'</pre></body></html>';
  }
}
function toggleInspector(){
  if(visualEditorActive){
    visualEditorActive=false;
    var vb=document.getElementById('visEditBtn');if(vb)vb.classList.remove('toolbar-btn-primary');
  }
  inspectorActive=!inspectorActive;
  var btn=document.getElementById('inspectBtn');
  if(btn)btn.classList.toggle('toolbar-btn-primary',inspectorActive);
  var info=document.getElementById('inspectInfo');
  if(info)info.style.display=inspectorActive?'block':'none';
  if(inspectorActive)showToast('🎯 Инспектор включён — наведите на элемент и кликните');
  refreshPreview();
}
function getInspectorScript(){
  var t=document.getElementById('ve-inspector-script');
  return t?t.textContent:'';
}
function addInspectorEntry(info){
  var panel=document.getElementById('inspectInfo');
  if(!panel||!info)return;
  panel.style.display='block';
  var esc2=function(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')};
  panel.innerHTML='<b style="color:var(--accent)">&lt;'+esc2(info.tag)+'&gt;</b>'+
    (info.id?' <span style="color:var(--orange)">#'+esc2(info.id)+'</span>':'')+
    (info.classes?' <span style="color:var(--green)">.'+esc2(info.classes)+'</span>':'')+
    ' <span style="color:var(--text3)">'+esc2(info.size)+'</span>'+
    ' <span style="color:var(--text2)">| display:'+esc2(info.display)+' position:'+esc2(info.position)+'</span>'+
    (info.text?' <span style="color:var(--text3)">| '+esc2(info.text)+'</span>':'');
}
/* ============================================================
   ВИЗУАЛЬНЫЙ РЕДАКТОР (панель в хост-странице)
   ------------------------------------------------------------
   Раньше панель редактора жила ВНУТРИ iframe превью и была покрашена
   в «чёрный GitHub» (#0d1117 / #6366f1): она не следовала ни за
   светлой темой редактора (body.light), ни за палитрой сайта, и
   перехватывала все клики — редактируемое приложение становилось
   некликабельным. «Применить в код» к тому же только дописывал
   текст в буфер редактора (до файла дело не доходило), а sync()
   сериализовал рантайм-DOM, из-за чего в .css/.js-вкладки попадал
   HTML-документ, а мёртвая модалка стилей (visStyleModal) была
   недостижима — отправлять visEditorStyle было некому.

   Теперь:
     * панель — в хост-странице, оформлена переменными темы, поэтому
       светлая/тёмная тема и палитра сайта соблюдаются;
     * в iframe — тонкий агент: подсветка, рамка выбора, хендлы
       изменения размера, правка текста двойным кликом;
     * правки пишутся в ИСХОДНЫЙ HTML (DOM-модель хоста), а не в
       сериализованный живой DOM: в файл попадает только авторский
       код, и CSS/JS-вкладки не портятся;
     * режим «Взаимодействие» — приложение работает как обычно;
     * кнопка «Сохранить» пишет файл через /api/apps/file/save.
   ============================================================ */

const VE_UNITS={width:'px',height:'px',padding:'px',margin:'px',gap:'px',
                borderRadius:'px',fontSize:'px',borderWidth:'px'};
const VE_SECTIONS=[
  {title:'Содержимое',props:[
    {p:'__text',label:'Текст',type:'textarea'},
    {p:'id',label:'id',type:'text'},
    {p:'class',label:'class',type:'text'}]},
  {title:'Размеры и отступы',props:[
    {p:'width',label:'Ширина',type:'text'},
    {p:'height',label:'Высота',type:'text'},
    {p:'padding',label:'Отступы',type:'text'},
    {p:'margin',label:'Поля',type:'text'},
    {p:'gap',label:'Gap',type:'text'},
    {p:'borderRadius',label:'Скругление',type:'text'}]},
  {title:'Типографика',props:[
    {p:'fontSize',label:'Размер шрифта',type:'text'},
    {p:'fontWeight',label:'Начертание',type:'select',opts:[['','auto'],['300','Light'],['400','Normal'],['500','Medium'],['600','Semibold'],['700','Bold'],['800','Extra bold']]},
    {p:'textAlign',label:'Выравнивание',type:'select',opts:[['','auto'],['left','Слева'],['center','Центр'],['right','Справа'],['justify','По ширине']]},
    {p:'color',label:'Цвет текста',type:'color'},
    {p:'backgroundColor',label:'Цвет фона',type:'color'}]},
  {title:'Блок',props:[
    {p:'display',label:'Display',type:'select',opts:[['','auto'],['block','block'],['inline','inline'],['inline-block','inline-block'],['flex','flex'],['grid','grid'],['none','none']]},
    {p:'position',label:'Position',type:'select',opts:[['','auto'],['static','static'],['relative','relative'],['absolute','absolute'],['fixed','fixed'],['sticky','sticky']]},
    {p:'opacity',label:'Прозрачность',type:'text'},
    {p:'zIndex',label:'z-index',type:'text'}]},
  {title:'Рамка и тень',props:[
    {p:'borderWidth',label:'Рамка',type:'text'},
    {p:'borderColor',label:'Цвет рамки',type:'color'},
    {p:'boxShadow',label:'Тень',type:'text'}]}
];
const veState={doc:null,selId:null,mode:'select',undo:[],redo:[],lastBuffer:'',marked:''};

function veBuffer(){
  if(activeTab<0||!openTabs[activeTab])return '';
  if(useMonaco&&monacoInstance)return monacoInstance.getValue();
  if(codeMirror)return codeMirror.getValue();
  return openTabs[activeTab].content||'';
}
function veIsHtmlTab(){
  if(activeTab<0||!openTabs[activeTab])return false;
  return /\.(html?|htm)$/i.test(openTabs[activeTab].name||'');
}
function veAssignIds(doc){
  var i=0;
  doc.body.querySelectorAll('*').forEach(function(el){ el.setAttribute('data-ve-id','v'+(++i)); });
  return doc;
}
function veDocHtml(doc,strip){
  var clone=doc.documentElement.cloneNode(true);
  if(strip)clone.querySelectorAll('[data-ve-id]').forEach(function(n){ n.removeAttribute('data-ve-id'); });
  return '<!DOCTYPE html>\n'+clone.outerHTML;
}
function veEl(id){
  if(!veState.doc||!id)return null;
  return veState.doc.querySelector('[data-ve-id="'+id+'"]');
}
function veSend(cmd,payload){
  var frame=document.getElementById('previewFrame');
  if(!frame||!frame.contentWindow)return;
  var msg={source:'veCmd',cmd:cmd};
  if(payload)for(var k in payload)msg[k]=payload[k];
  try{ frame.contentWindow.postMessage(msg,'*'); }catch(e){}
}
function veRenderFields(){
  var body=document.getElementById('veBody');
  if(!body)return;
  var html='';
  VE_SECTIONS.forEach(function(sec){
    html+='<div class="ve-sec">'+sec.title+'</div><div class="ve-grid">';
    sec.props.forEach(function(f){
      html+='<label class="ve-field" '+(f.p==='__text'?'style="grid-column:1/-1"':'')+'><span>'+f.label+'</span>';
      if(f.type==='select'){
        html+='<select data-ve-prop="'+f.p+'">';
        f.opts.forEach(function(o){ html+='<option value="'+o[0]+'">'+o[1]+'</option>'; });
        html+='</select>';
      }else if(f.type==='textarea'){
        html+='<textarea data-ve-prop="'+f.p+'"></textarea>';
      }else{
        html+='<input type="'+(f.type==='color'?'color':'text')+'" data-ve-prop="'+f.p+'">';
      }
      html+='</label>';
    });
    html+='</div>';
  });
  body.innerHTML=html;
  body.querySelectorAll('[data-ve-prop]').forEach(function(inp){
    inp.addEventListener('change',function(){ veSetProp(inp.getAttribute('data-ve-prop'),inp.value); });
  });
}
function veFillFields(){
  var el=veEl(veState.selId);
  var body=document.getElementById('veBody');
  if(!body)return;
  body.querySelectorAll('[data-ve-prop]').forEach(function(inp){
    var p=inp.getAttribute('data-ve-prop');
    var v='';
    if(el){
      if(p==='__text')v=el.textContent||'';
      else if(p==='id')v=el.getAttribute('id')||'';
      else if(p==='class')v=el.getAttribute('class')||'';
      else v=el.style[p]||'';
    }
    inp.value=v;
  });
}
function veApplyStyleToEl(el,prop,value){
  if(!el)return;
  if(prop==='__text'){ el.textContent=value; return; }
  if(prop==='id'){ if(value)el.setAttribute('id',value); else el.removeAttribute('id'); return; }
  if(prop==='class'){ el.setAttribute('class',value||''); return; }
  var v=value;
  if(v===''||v==null){ el.style[prop]=''; return; }
  var unit=VE_UNITS[prop];
  if(unit&&/^-?\d+(\.\d+)?$/.test(String(v)))v=v+unit;
  el.style[prop]=v;
}
function vePushUndo(){
  veState.undo.push(veState.lastBuffer);
  if(veState.undo.length>60)veState.undo.shift();
  veState.redo=[];
}
function veCommit(){
  if(!veState.doc)return;
  var html=veDocHtml(veState.doc,true);
  veState.lastBuffer=html;
  if(activeTab>=0&&openTabs[activeTab])openTabs[activeTab].content=html;
  if(useMonaco&&monacoInstance)monacoInstance.setValue(html);
  else if(codeMirror)codeMirror.setValue(html);
  veState.marked=veDocHtml(veState.doc,false);
}
function vePrepare(){
  var html=veBuffer();
  var doc=new DOMParser().parseFromString(html,'text/html');
  veState.doc=veAssignIds(doc);
  veState.lastBuffer=html;
  veState.marked=veDocHtml(veState.doc,false);
  veState.selId=null;
  veRenderFields();
  veFillFields();
  var crumb=document.getElementById('veCrumb');
  if(crumb)crumb.textContent='Элемент не выбран';
}
function toggleVisualEditor(){
  if(visualEditorActive){ veClose(); return; }
  if(!veIsHtmlTab()){
    showToast('Визуальный редактор работает только с HTML-файлами','error');
    return;
  }
  visualEditorActive=true;
  veState.mode='select';
  veState.undo=[];veState.redo=[];
  var btn=document.getElementById('visEditBtn');
  if(btn)btn.classList.add('toolbar-btn-primary');
  var panel=document.getElementById('vePanel');
  if(panel)panel.classList.add('show');
  veSetMode('select');
  vePrepare();
  refreshPreview();
  updateResizers();
  showToast('✏️ Визуальный редактор: клик — выбрать элемент, двойной клик — текст');
}
function veClose(quiet){
  visualEditorActive=false;
  var btn=document.getElementById('visEditBtn');
  if(btn)btn.classList.remove('toolbar-btn-primary');
  var panel=document.getElementById('vePanel');
  if(panel)panel.classList.remove('show');
  veState.doc=null;veState.selId=null;veState.marked='';
  updateResizers();
  refreshPreview();
  if(!quiet)showToast('Визуальный редактор выключен');
}
function veSetMode(mode){
  veState.mode=mode;
  var s=document.getElementById('veModeSelect');
  var i=document.getElementById('veModeInteract');
  if(s)s.classList.toggle('active',mode==='select');
  if(i)i.classList.toggle('active',mode==='interact');
  veSend('mode',{mode:mode});
}
function veSetProp(prop,value){
  var el=veEl(veState.selId);
  if(!el)return;
  vePushUndo();
  veApplyStyleToEl(el,prop,value);
  veCommit();
  veFillFields();
  veSend('style',{id:veState.selId,prop:prop,value:value});
}
function veOnSelect(data){
  veState.selId=data.id||null;
  var crumb=document.getElementById('veCrumb');
  if(crumb)crumb.textContent=data.crumb||'Элемент не выбран';
  veFillFields();
}
function veOnText(data){
  var el=veEl(data.id);
  if(!el)return;
  vePushUndo();
  el.textContent=data.text||'';
  veCommit();
  veFillFields();
}
function veOnStyle(data){
  var el=veEl(data.id);
  if(!el||!data.props)return;
  vePushUndo();
  for(var k in data.props)veApplyStyleToEl(el,k,data.props[k]);
  veCommit();
  veFillFields();
}
function veUndo(){
  if(!veState.undo.length){ showToast('Нечего отменять','error'); return; }
  veState.redo.push(veState.lastBuffer);
  var html=veState.undo.pop();
  veApplyBuffer(html);
}
function veRedo(){
  if(!veState.redo.length){ showToast('Нечего повторять','error'); return; }
  veState.undo.push(veState.lastBuffer);
  var html=veState.redo.pop();
  veApplyBuffer(html);
}
function veApplyBuffer(html){
  veState.doc=veAssignIds(new DOMParser().parseFromString(html,'text/html'));
  veState.lastBuffer=html;
  if(activeTab>=0&&openTabs[activeTab])openTabs[activeTab].content=html;
  if(useMonaco&&monacoInstance)monacoInstance.setValue(html);
  else if(codeMirror)codeMirror.setValue(html);
  veState.marked=veDocHtml(veState.doc,false);
  veFillFields();
  refreshPreview();
}
async function veAddElement(){
  if(!veState.doc){ showToast('\u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 HTML-\u0444\u0430\u0439\u043b','error'); return; }
  var D=window.InfinityDialogs;
  var tag='div', text='\u041d\u043e\u0432\u044b\u0439 \u0431\u043b\u043e\u043a';
  if(D&&D.form){
    var res=await D.form({
      title:'\u041d\u043e\u0432\u044b\u0439 \u044d\u043b\u0435\u043c\u0435\u043d\u0442',icon:'fas fa-plus',okText:'\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c',
      fields:[{name:'tag',label:'\u0422\u0435\u0433 \u044d\u043b\u0435\u043c\u0435\u043d\u0442\u0430',value:'div',required:true},
              {name:'text',label:'\u0422\u0435\u043a\u0441\u0442',value:'\u041d\u043e\u0432\u044b\u0439 \u0431\u043b\u043e\u043a'}]
    });
    if(!res)return;
    tag=String(res.tag||'div').trim().toLowerCase().replace(/[^a-z0-9-]/g,'')||'div';
    text=String(res.text==null?'':res.text);
  }
  var parent=veEl(veState.selId)||veState.doc.body;
  if(!parent)return;
  vePushUndo();
  var id='v'+(veState.doc.querySelectorAll('*').length+1);
  var el=veState.doc.createElement(tag);
  el.setAttribute('data-ve-id',id);
  el.textContent=text;
  parent.appendChild(el);
  veState.selId=id;
  veCommit();
  veFillFields();
  veSend('insert',{id:id,tag:tag,text:text,parentId:parent.getAttribute('data-ve-id')||''});
  showToast('\u2713 \u042d\u043b\u0435\u043c\u0435\u043d\u0442 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d');
}
function veRemoveById(id){
  var el=veEl(id);
  if(!el)return;
  vePushUndo();
  el.remove();
  if(veState.selId===id)veState.selId=null;
  veCommit();
  veFillFields();
  var crumb=document.getElementById('veCrumb');
  if(crumb)crumb.textContent='\u042d\u043b\u0435\u043c\u0435\u043d\u0442 \u043d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d';
  veSend('remove',{id:id});
}
function veDeleteElement(){
  var el=veEl(veState.selId);
  if(!el){ showToast('Элемент не выбран','error'); return; }
  vePushUndo();
  var id=veState.selId;
  el.remove();
  veState.selId=null;
  veCommit();
  veFillFields();
  var crumb=document.getElementById('veCrumb');
  if(crumb)crumb.textContent='Элемент не выбран';
  veSend('remove',{id:id});
}
function veApplyToCode(){
  if(!veState.doc)return;
  veCommit();
  showToast('✓ Элемент добавлен');
}
function veSave(){
  if(!veState.doc){ showToast('Визуальный редактор не активен','error'); return; }
  veCommit();
  saveCurrentFile();
}

function exportPreviewHtml(){
  var html=(veState.doc)?veDocHtml(veState.doc,true):veBuffer();
  if(!html){showToast('Превью не активно','error');return;}
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(html).then(function(){showToast('✓ HTML скопирован в буфер');}).catch(function(){showToast('Ошибка копирования','error');});
  }else{
    var ta=document.createElement('textarea');ta.value=html;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');showToast('✓ HTML скопирован в буфер');}catch(e){}
    ta.remove();
  }
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

async function exportZip(){
  if(!files||!files.length){showToast('Нет файлов для экспорта','error');return;}
  if(typeof JSZip==='undefined'){showToast('JSZip не загружен','error');return;}
  var zip=new JSZip();
  // /api/apps/files отдаёт список без содержимого файла,
  // раньше в архив попадали пустые файлы были пустыми.
  var list=files.filter(function(f){return f.path&&!f.is_dir;});
  for(var i=0;i<list.length;i++){
    var f=list[i];
    var content=f.content;
    if(content===undefined||content===null){
      try{
        var r=await fetch('/api/apps/file/get?id='+encodeURIComponent(appId)+'&path='+encodeURIComponent(f.path));
        var d=await r.json();
        content=d&&d.content!==undefined?d.content:'';
      }catch(e){content='';}
    }
    zip.file(f.path,content||'');
  }
  var appDirName=appDir?appDir.split('/').pop()||appDir.split('\\').pop()||'project':'project';
  zip.generateAsync({type:'blob'}).then(function(blob){
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download=appDirName+'.zip';
    document.body.appendChild(a);a.click();
    setTimeout(function(){document.body.removeChild(a);URL.revokeObjectURL(a.href);},100);
    showToast('✓ Архив '+appDirName+'.zip скачан');
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
    const r=await fetch('/api/apps/file/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId,path:fullPath,content})});
    if(!r.ok){try{const d=await r.json();alert('Ошибка: '+(d.error||r.statusText))}catch(e){alert('Ошибка: '+r.statusText)};return}
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
    const r=await fetch('/api/apps/folder/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId,path:fullPath})});
    if(!r.ok){const d=await r.json();alert('Ошибка: '+(d.error||r.statusText));return}
    showToast('✓ Папка создана: '+fullPath);
    document.querySelector('.file-modal-overlay')?.remove();
    loadFiles();
  }catch(e){alert(e.message)}
}

async function createNewFile(){showFileModal()}
async function createNewFolder(){showFolderModal()}

function loadFiles(){
  fetch('/api/apps/files?id='+appId).then(r=>r.json()).then(d=>{
    files=d.files||[];
    fileTree=buildTree(files);
    if(d.dir)appDir=d.dir;
    if(d.manifest)appManifest=d.manifest;
    document.getElementById('appTitle').textContent=d.name||appId;
    renderFileTree();
    checkServerStatus();
  }).catch(e=>{
    console.error('loadFiles error:',e);
    showToast('Ошибка загрузки файлов');
  });
}

async function deleteFile(path){
  if(!confirm('Удалить '+path+'?'))return;
  try{
    const r=await fetch('/api/apps/file/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId,path})});
    if(!r.ok){const d=await r.json();alert('Ошибка: '+(d.error||r.statusText));return}
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
  try{localStorage.setItem('appEditLayout',JSON.stringify(layout));}catch(e){}
}
function loadLayout(){
  try{
    var data=JSON.parse(localStorage.getItem('appEditLayout'));
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
  var rzv=document.getElementById('resizerVe');
  if(rzv)rzv.style.display=document.getElementById('vePanel').classList.contains('show')?'':'none';
}

// === App Settings ===
function openAppSettings(){
  document.getElementById('settingsName').value=appManifest.name||appId;
  document.getElementById('settingsType').value=appManifest.type||'html';
  document.getElementById('settingsDesc').value=appManifest.description||'';
  document.getElementById('settingsIcon').value=appManifest.icon||'';
  document.getElementById('settingsCover').value=appManifest.cover||'';
  document.getElementById('settingsMain').value=appManifest.main||'index.html';
  document.getElementById('settingsUrl').value=appManifest.url||'';
  document.getElementById('appSettingsModal').style.display='flex';
}
function closeAppSettings(){document.getElementById('appSettingsModal').style.display='none'}
async function saveAppSettings(){
  const data={
    id:appId,
    name:document.getElementById('settingsName').value,
    type:document.getElementById('settingsType').value,
    description:document.getElementById('settingsDesc').value,
    icon:document.getElementById('settingsIcon').value,
    cover:document.getElementById('settingsCover').value,
    main:document.getElementById('settingsMain').value,
    url:document.getElementById('settingsUrl').value
  };
  try{
    const r=await fetch('/api/apps/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    if(d.ok){
      appManifest=data;
      document.getElementById('appTitle').textContent=data.name;
      closeAppSettings();
      showToast('✓ Настройки сохранены');
    }else{showToast('Ошибка: '+(d.error||''))}
  }catch(e){showToast('Ошибка: '+e.message)}
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
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#x27;')}
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
    }else if(e.data&&e.data.source==='veInspect'){
      addInspectorEntry(e.data.info);
    }else if(e.data&&e.data.source==='veSelect'){
      veOnSelect(e.data);
    }else if(e.data&&e.data.source==='veText'){
      veOnText(e.data);
    }else if(e.data&&e.data.source==='veStyle'){
      veOnStyle(e.data);
    }else if(e.data&&e.data.source==='veRemove'){
      veRemoveById(e.data.id);
    }else if(e.data&&e.data.source==='veToast'){
      showToast(e.data.msg||'');
    }else if(e.data&&e.data.source==='veExit'){
      if(visualEditorActive)toggleVisualEditor();
    }else if(e.data&&e.data.source==='veImgLib'&&e.data.action==='open'){
      showImgLibDialog();
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
      var url='/api/apps/serve?f='+encodeURIComponent(appDir)+'&p='+encodeURIComponent(img.path);
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
      var fd=new FormData();
      fd.append('appId',appId);
      fd.append('file',files[i]);
      try{
        var r=await fetch('/api/apps/image/upload',{method:'POST',body:fd});
        var d=await r.json();
        if(d.error)showToast('Ошибка: '+d.error,'error');
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

// ============ APP CONFIG ============
var appConfigData={staticDirectories:[],staticFiles:[],routes:[],cors:{enabled:false,origins:'*'}};

function toggleConfigPanel(){
  var p=document.getElementById('configPanel');
  p.classList.toggle('show');
  updateResizers();
  if(p.classList.contains('show')&&!document.getElementById('configForm').children.length){
    loadAppConfig();
  }
}

async function loadAppConfig(){
  var status=document.getElementById('configStatus');
  try{
    var r=await fetch('/api/apps/config/get?id='+appId);
    var d=await r.json();
    if(d.error)throw new Error(d.error);
    appConfigData=d.config||{staticDirectories:[],staticFiles:[],routes:[],cors:{enabled:false,origins:'*'}};
    renderConfigForm();
    status.className='config-status';
    status.style.display='none';
  }catch(e){
    status.className='config-status err';
    status.textContent='Ошибка загрузки: '+e.message;
    status.style.display='block';
  }
}

async function saveAppConfig(){
  var status=document.getElementById('configStatus');
  collectConfigForm();
  try{
    var r=await fetch('/api/apps/config/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:appId,config:appConfigData})});
    var d=await r.json();
    if(d.error)throw new Error(d.error);
    status.className='config-status ok';
    status.textContent='✓ Конфигурация сохранена';
    status.style.display='block';
    setTimeout(function(){status.style.display='none'},3000);
  }catch(e){
    status.className='config-status err';
    status.textContent='Ошибка сохранения: '+e.message;
    status.style.display='block';
  }
}

function collectConfigForm(){
  appConfigData.name=document.getElementById('cfgName')?.value||'';
  appConfigData.type=document.getElementById('cfgType')?.value||'html';
  appConfigData.description=document.getElementById('cfgDesc')?.value||'';
  appConfigData.main=document.getElementById('cfgMain')?.value||'index.html';
  appConfigData.icon=document.getElementById('cfgIcon')?.value||'';
  appConfigData.cover=document.getElementById('cfgCover')?.value||'';
  appConfigData.url=document.getElementById('cfgUrl')?.value||'';
  // Read dynamic fields from input values
  var dirs=document.querySelectorAll('[data-array="staticDirs"]');
  appConfigData.staticDirectories=[];
  dirs.forEach(function(inp){if(inp.value.trim())appConfigData.staticDirectories.push(inp.value.trim());});
  var files=document.querySelectorAll('[data-array="staticFiles"]');
  appConfigData.staticFiles=[];
  files.forEach(function(inp){if(inp.value.trim())appConfigData.staticFiles.push(inp.value.trim());});
  // Routes
  var routePaths=document.querySelectorAll('[data-array="routePath"]');
  var routeDirs=document.querySelectorAll('[data-array="routeDir"]');
  appConfigData.routes=[];
  for(var i=0;i<routePaths.length;i++){
    var p=routePaths[i].value.trim();
    var d=routeDirs[i]?routeDirs[i].value.trim():'';
    if(p)appConfigData.routes.push({path:p,directory:d});
  }
  // CORS
  appConfigData.cors={enabled:document.getElementById('cfgCors').checked,origins:document.getElementById('cfgCorsOrigins').value};
}

function renderConfigForm(){
  var c=appConfigData;
  var html='<div class="config-section"><div class="config-section-title">📋 manifest.json</div>';
  html+='<div class="config-row"><div class="config-field"><label>Название</label><input id="cfgName" value="'+esc(c.name||'')+'"></div>';
  html+='<div class="config-field"><label>Тип</label><select id="cfgType"><option value="html"'+(c.type==='html'?' selected':'')+'>HTML</option><option value="python"'+(c.type==='python'?' selected':'')+'>Python</option><option value="link"'+(c.type==='link'?' selected':'')+'>Ссылка</option><option value="bg"'+(c.type==='bg'||c.type==='background'?' selected':'')+'>Фоновое</option></select></div></div>';
  html+='<div class="config-field"><label>Описание</label><textarea id="cfgDesc" rows="2">'+esc(c.description||'')+'</textarea></div>';
  html+='<div class="config-row"><div class="config-field"><label>Иконка</label><input id="cfgIcon" value="'+esc(c.icon||'')+'" placeholder="emoji или URL"></div>';
  html+='<div class="config-field"><label>Обложка</label><input id="cfgCover" value="'+esc(c.cover||'')+'" placeholder="URL"></div></div>';
  html+='<div class="config-row"><div class="config-field"><label>Главный файл</label><input id="cfgMain" value="'+esc(c.main||'index.html')+'"></div>';
  html+='<div class="config-field"><label>URL (для ссылок)</label><input id="cfgUrl" value="'+esc(c.url||'')+'" placeholder="https://"></div></div>';

  html+='</div><div class="config-section"><div class="config-section-title">📁 app.config.json</div>';

  // Static Directories
  html+='<div class="config-section-title" style="font-size:11px;margin-top:12px">🗂️ Статические папки</div>';
  html+='<div id="staticDirsList">';
  (c.staticDirectories||[]).forEach(function(dir,i){
    html+='<div class="config-array-item"><input data-array="staticDirs" value="'+esc(dir)+'" placeholder="./public"><button class="rm-btn" onclick="removeConfigItem(this,\'staticDirs\')">✕</button></div>';
  });
  html+='</div><button class="config-add-btn" onclick="addConfigItem(\'staticDirs\')">+ Добавить папку</button>';

  // Static Files
  html+='<div class="config-section-title" style="font-size:11px;margin-top:12px">📄 Статические файлы</div>';
  html+='<div id="staticFilesList">';
  (c.staticFiles||[]).forEach(function(file,i){
    html+='<div class="config-array-item"><input data-array="staticFiles" value="'+esc(file)+'" placeholder="./favicon.ico"><button class="rm-btn" onclick="removeConfigItem(this,\'staticFiles\')">✕</button></div>';
  });
  html+='</div><button class="config-add-btn" onclick="addConfigItem(\'staticFiles\')">+ Добавить файл</button>';

  // Routes
  html+='<div class="config-section-title" style="font-size:11px;margin-top:12px">🔗 Кастомные маршруты</div>';
  html+='<div id="routesList">';
  (c.routes||[]).forEach(function(route,i){
    html+='<div class="config-array-item"><input data-array="routePath" value="'+esc(route.path||'')+'" placeholder="/api/data" style="flex:0 0 40%"><input data-array="routeDir" value="'+esc(route.directory||'')+'" placeholder="./data" style="flex:0 0 40%"><button class="rm-btn" onclick="removeConfigItem(this,\'routes\')">✕</button></div>';
  });
  html+='</div><button class="config-add-btn" onclick="addConfigItem(\'routes\')">+ Добавить маршрут</button>';

  // CORS
  html+='<div class="config-section-title" style="font-size:11px;margin-top:12px">🌐 CORS</div>';
  html+='<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><label style="font-size:12px"><input id="cfgCors" type="checkbox"'+(c.cors&&c.cors.enabled?' checked':'')+'> Включить CORS</label></div>';
  html+='<div class="config-field"><label>Разрешённые источники</label><input id="cfgCorsOrigins" value="'+esc(c.cors&&c.cors.origins||'*')+'"></div>';

  html+='</div>';
  document.getElementById('configForm').innerHTML=html;
}

function addConfigItem(type){
  var list=document.getElementById(type+'List');
  if(type==='staticDirs'){
    list.insertAdjacentHTML('beforeend','<div class="config-array-item"><input data-array="staticDirs" value="" placeholder="./public"><button class="rm-btn" onclick="removeConfigItem(this,\'staticDirs\')">✕</button></div>');
  }else if(type==='staticFiles'){
    list.insertAdjacentHTML('beforeend','<div class="config-array-item"><input data-array="staticFiles" value="" placeholder="./favicon.ico"><button class="rm-btn" onclick="removeConfigItem(this,\'staticFiles\')">✕</button></div>');
  }else if(type==='routes'){
    list.insertAdjacentHTML('beforeend','<div class="config-array-item"><input data-array="routePath" value="" placeholder="/api/data" style="flex:0 0 40%"><input data-array="routeDir" value="" placeholder="./data" style="flex:0 0 40%"><button class="rm-btn" onclick="removeConfigItem(this,\'routes\')">✕</button></div>');
  }
}

function removeConfigItem(btn,type){
  btn.closest('.config-array-item').remove();
}
</script>
<script type="text/template" id="ve-inspector-script">
(function(){
  if(window.__veInspector)return;window.__veInspector=true;
  function mk(css){var d=document.createElement('div');d.setAttribute('data-ve','1');d.style.cssText=css;document.documentElement.appendChild(d);return d;}
  var hov=mk('position:fixed;z-index:2147483000;pointer-events:none;border:1px dashed #6366f1;background:rgba(99,102,241,0.08);display:none;box-sizing:border-box;border-radius:2px');
  var lab=mk('position:fixed;z-index:2147483001;pointer-events:none;background:#111827;color:#e5e7eb;font:11px/1.5 monospace;padding:3px 7px;border-radius:5px;border:1px solid rgba(255,255,255,.15);display:none;max-width:80vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis');
  function rgb2hex(v){
    if(!v)return '';
    var m=String(v).match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
    if(!m)return v;
    return '#'+('0'+parseInt(m[1]).toString(16)).slice(-2)+('0'+parseInt(m[2]).toString(16)).slice(-2)+('0'+parseInt(m[3]).toString(16)).slice(-2);
  }
  function describe(el){
    if(!el||el.nodeType!==1)return '';
    var t=el.tagName.toLowerCase();
    if(el.id)t+='#'+el.id;
    if(el.className&&typeof el.className==='string'){
      var cls=el.className.trim().split(/\s+/).filter(Boolean).slice(0,2);
      if(cls.length)t+='.'+cls.join('.');
    }
    return t;
  }
  function info(el){
    var s=getComputedStyle(el),r=el.getBoundingClientRect();
    return {
      tag:el.tagName.toLowerCase(),
      id:el.id||'',
      classes:(typeof el.className==='string'?el.className:'').trim(),
      size:Math.round(r.width)+'x'+Math.round(r.height),
      display:s.display,position:s.position,
      color:rgb2hex(s.color),bg:rgb2hex(s.backgroundColor),
      font:s.fontSize+' '+s.fontFamily,fontWeight:s.fontWeight,
      padding:s.paddingTop+' '+s.paddingRight+' '+s.paddingBottom+' '+s.paddingLeft,
      margin:s.marginTop+' '+s.marginRight+' '+s.marginBottom+' '+s.marginLeft,
      border:s.borderWidth+' '+s.borderStyle+' '+s.borderColor,
      borderRadius:s.borderRadius,opacity:s.opacity,zIndex:s.zIndex,
      boxShadow:s.boxShadow,
      text:(el.textContent||'').trim().replace(/\s+/g,' ').slice(0,120)
    };
  }
  function place(node,el){
    var r=el.getBoundingClientRect();
    node.style.left=r.left+'px';node.style.top=r.top+'px';
    node.style.width=r.width+'px';node.style.height=r.height+'px';
  }
  function hideAll(){hov.style.display='none';lab.style.display='none';}
  function onMove(e){
    var el=e.target;
    if(!el||el.nodeType!==1||el.closest('[data-ve]'))return;
    place(hov,el);hov.style.display='block';
    lab.textContent=describe(el);
    lab.style.display='block';
    var r=el.getBoundingClientRect();
    lab.style.left=Math.max(2,Math.min(r.left,window.innerWidth-120))+'px';
    lab.style.top=(r.top>24?r.top-22:r.bottom+2)+'px';
  }
  function onClick(e){
    var el=e.target;
    if(!el||el.nodeType!==1||el.closest('[data-ve]'))return;
    e.preventDefault();e.stopPropagation();
    try{window.parent.postMessage({source:'veInspect',info:info(el)},'*');}catch(err){}
  }
  document.addEventListener('mousemove',onMove,true);
  document.addEventListener('click',onClick,true);
  document.addEventListener('scroll',hideAll,true);
  window.addEventListener('resize',hideAll);
})();
</script>
<script type="text/template" id="ve-editor-script">
/* Агент визуального редактора — тонкий слой внутри превью.
   Своей панели здесь больше нет: она живёт в хост-странице и оформлена
   переменными темы. Агент отвечает только за подсветку, выбор
   элемента, хендлы размера и правку текста — и сообщает хосту, что
   изменилось. Правки в файле делает хост (по исходному HTML), поэтому
   рантайм-DOM приложения в файл не попадает. */
(function(){
  if(window.__veAgent)return;window.__veAgent=true;
  var MODE='select';
  var SEL=null;
  function send(source,data){
    var m={source:source};
    if(data)for(var k in data)m[k]=data[k];
    try{parent.postMessage(m,'*');}catch(e){}
  }
  function mk(t,css){
    var d=document.createElement(t);
    d.setAttribute('data-ve','1');
    if(css)d.style.cssText=css;
    document.documentElement.appendChild(d);
    return d;
  }
  var hover=mk('div','position:fixed;z-index:2147483000;pointer-events:none;border:1px dashed rgba(99,102,241,.9);background:rgba(99,102,241,.06);display:none;box-sizing:border-box;border-radius:2px');
  var box=mk('div','position:fixed;z-index:2147483002;pointer-events:none;border:2px solid #6366f1;display:none;box-sizing:border-box;border-radius:2px');
  var tag=mk('div','position:fixed;z-index:2147483003;pointer-events:none;background:#6366f1;color:#fff;font:11px/1.4 monospace;padding:2px 6px;border-radius:4px 4px 0 0;display:none;max-width:70vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis');
  var handles=[];
  var dirs=[['nw',-5,-5,'nwse-resize'],['n',-5,'50%','ns-resize'],['ne','calc(100% - 5px)',-5,'nesw-resize'],
            ['e','calc(100% - 5px)','50%','ew-resize'],['se','calc(100% - 5px)','calc(100% - 5px)','nwse-resize'],
            ['s','50%','calc(100% - 5px)','ns-resize'],['sw',-5,'calc(100% - 5px)','nesw-resize'],
            ['w',-5,'50%','ew-resize']];
  dirs.forEach(function(d){
    var q=document.createElement('div');
    q.setAttribute('data-ve','1');
    q.setAttribute('data-dir',d[0]);
    q.style.cssText='position:absolute;width:10px;height:10px;background:#6366f1;border:1.5px solid #fff;border-radius:2px;pointer-events:auto;box-sizing:border-box;cursor:'+d[3]+';display:none';
    box.appendChild(q);handles.push(q);
    q.addEventListener('mousedown',function(e){startResize(e,d[0]);});
  });
  function describe(el){
    if(!el||el.nodeType!==1)return '';
    var t=el.tagName.toLowerCase();
    if(el.id)t+='#'+el.id;
    if(el.className&&typeof el.className==='string'){
      var c=el.className.trim().split(/\s+/).filter(Boolean).slice(0,2);
      if(c.length)t+='.'+c.join('.');
    }
    return t;
  }
  function overlay(node,el){
    if(!el){node.style.display='none';return;}
    var r=el.getBoundingClientRect();
    node.style.display='block';
    node.style.left=r.left+'px';node.style.top=r.top+'px';
    node.style.width=r.width+'px';node.style.height=r.height+'px';
  }
  function crumbOf(el){
    var path=[],n=el;
    while(n&&n.nodeType===1&&path.length<6){path.unshift(describe(n));n=n.parentElement;}
    return path.join(' > ');
  }
  function stylesOf(el){
    var s=getComputedStyle(el);
    var out={};
    var present=['color','backgroundColor','display','position','opacity','zIndex','fontWeight','textAlign'];
    present.forEach(function(p){
      var v=el.style[p];
      if(!v){
        v=s[p];
        if(p==='opacity'&&v==='1')v='';
        if(p==='zIndex'&&v==='auto')v='';
      }
      out[p]=v||'';
    });
    ['width','height','padding','margin','gap','borderRadius','fontSize','borderWidth','boxShadow'].forEach(function(p){
      out[p]=el.style[p]||'';
    });
    return out;
  }
  function updateSel(){
    if(!SEL){box.style.display='none';tag.style.display='none';handles.forEach(function(q){q.style.display='none'});return;}
    overlay(box,SEL);
    var r=SEL.getBoundingClientRect();
    tag.style.display='block';tag.textContent=describe(SEL);
    tag.style.left=Math.max(0,r.left)+'px';
    tag.style.top=Math.max(0,r.top-20)+'px';
    handles.forEach(function(q){q.style.display='block'});
  }
  function report(){
    if(!SEL)return;
    send('veSelect',{
      id:SEL.getAttribute('data-ve-id')||'',
      tag:SEL.tagName.toLowerCase(),
      crumb:crumbOf(SEL),
      text:(SEL.innerText||'').slice(0,400),
      styles:stylesOf(SEL)
    });
  }
  function select(el){
    if(!el||el.nodeType!==1||el.closest('[data-ve]'))return;
    SEL=el;updateSel();report();
  }
  function startResize(e,dir){
    if(!SEL)return;
    e.preventDefault();e.stopPropagation();
    var r=SEL.getBoundingClientRect();
    var x0=e.clientX,y0=e.clientY,w0=r.width,h0=r.height;
    var pos0=getComputedStyle(SEL).position;
    function move(ev){
      var dx=ev.clientX-x0,dy=ev.clientY-y0;
      var w=w0,h=h0;
      if(dir.indexOf('e')>=0)w=Math.max(8,w0+dx);
      if(dir.indexOf('w')>=0)w=Math.max(8,w0-dx);
      if(dir.indexOf('s')>=0)h=Math.max(8,h0+dy);
      if(dir.indexOf('n')>=0)h=Math.max(8,h0-dy);
      SEL.style.width=Math.round(w)+'px';
      SEL.style.height=Math.round(h)+'px';
      if(pos0==='static')SEL.style.position='relative';
      if(dir.indexOf('w')>=0)SEL.style.left=Math.round((parseFloat(SEL.style.left)||0)-dx)+'px';
      if(dir.indexOf('n')>=0)SEL.style.top=Math.round((parseFloat(SEL.style.top)||0)-dy)+'px';
      updateSel();
    }
    function up(){
      document.removeEventListener('mousemove',move,true);
      document.removeEventListener('mouseup',up,true);
      send('veStyle',{id:SEL.getAttribute('data-ve-id')||'',props:{
        width:SEL.style.width,height:SEL.style.height,
        left:SEL.style.left,top:SEL.style.top,position:SEL.style.position}});
    }
    document.addEventListener('mousemove',move,true);
    document.addEventListener('mouseup',up,true);
  }
  function startDrag(e){
    if(!SEL||!e.altKey)return;
    e.preventDefault();e.stopPropagation();
    var x0=e.clientX,y0=e.clientY;
    var l0=parseFloat(SEL.style.left)||0,t0=parseFloat(SEL.style.top)||0;
    if(getComputedStyle(SEL).position==='static')SEL.style.position='relative';
    function move(ev){
      SEL.style.left=Math.round(l0+ev.clientX-x0)+'px';
      SEL.style.top=Math.round(t0+ev.clientY-y0)+'px';
      updateSel();
    }
    function up(){
      document.removeEventListener('mousemove',move,true);
      document.removeEventListener('mouseup',up,true);
      send('veStyle',{id:SEL.getAttribute('data-ve-id')||'',props:{
        left:SEL.style.left,top:SEL.style.top,position:SEL.style.position}});
    }
    document.addEventListener('mousemove',move,true);
    document.addEventListener('mouseup',up,true);
  }
  function textEdit(){
    if(!SEL)return;
    SEL.setAttribute('contenteditable','true');
    SEL.focus();
    try{
      var range=document.createRange();range.selectNodeContents(SEL);
      var sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);
    }catch(e){}
    function finish(){
      SEL.removeAttribute('contenteditable');
      SEL.removeEventListener('blur',finish);
      send('veText',{id:SEL.getAttribute('data-ve-id')||'',text:SEL.innerText||''});
    }
    SEL.addEventListener('blur',finish);
  }
  document.addEventListener('mousemove',function(e){
    if(MODE!=='select'){hover.style.display='none';return;}
    var el=e.target;
    if(!el||el.nodeType!==1||el.closest('[data-ve]')){hover.style.display='none';return;}
    overlay(hover,el);
  },true);
  document.addEventListener('click',function(e){
    if(MODE!=='select')return;                 // режим взаимодействия: клики приложения живые
    if(e.target.closest('[data-ve]'))return;
    e.preventDefault();e.stopPropagation();
    select(e.target);
  },true);
  document.addEventListener('dblclick',function(e){
    if(MODE!=='select')return;
    if(e.target.closest('[data-ve]'))return;
    e.preventDefault();e.stopPropagation();
    select(e.target);textEdit();
  },true);
  document.addEventListener('mousedown',function(e){
    if(MODE!=='select')return;
    if(e.target.closest('[data-ve]'))return;
    if(e.altKey){select(e.target);startDrag(e);}
  },true);
  document.addEventListener('keydown',function(e){
    if(MODE!=='select')return;
    var t=e.target&&e.target.tagName;
    if(t==='INPUT'||t==='TEXTAREA'||(e.target&&e.target.isContentEditable))return;
    if((e.key==='Delete'||e.key==='Backspace')&&SEL){
      e.preventDefault();
      send('veRemove',{id:SEL.getAttribute('data-ve-id')||''});
      SEL=null;updateSel();
    }
  });
  window.addEventListener('message',function(e){
    var d=e.data||{};
    if(d.source!=='veCmd')return;
    if(d.cmd==='mode'){MODE=d.mode||'select';return;}
    if(d.cmd==='style'&&d.id){
      var el=document.querySelector('[data-ve-id="'+d.id+'"]');
      if(!el)return;
      if(d.prop==='__text')el.textContent=d.value||'';
      else if(d.prop==='id'){if(d.value)el.setAttribute('id',d.value);else el.removeAttribute('id');}
      else if(d.prop==='class')el.setAttribute('class',d.value||'');
      else el.style[d.prop]=d.value||'';
      if(SEL===el)updateSel();
      return;
    }
    if(d.cmd==='text'&&d.id){
      var t2=document.querySelector('[data-ve-id="'+d.id+'"]');
      if(t2){t2.textContent=d.text||'';if(SEL===t2)updateSel();}
      return;
    }
    if(d.cmd==='remove'&&d.id){
      var rm=document.querySelector('[data-ve-id="'+d.id+'"]');
      if(rm)rm.remove();
      if(SEL&&SEL.getAttribute('data-ve-id')===d.id){SEL=null;updateSel();}
      return;
    }
    if(d.cmd==='insert'){
      var parent=d.parentId?document.querySelector('[data-ve-id="'+d.parentId+'"]'):document.body;
      if(!parent)parent=document.body;
      var ne=document.createElement(d.tag||'div');
      ne.setAttribute('data-ve-id',d.id||'');
      ne.textContent=d.text||'';
      parent.appendChild(ne);
      return;
    }
  });
  document.addEventListener('scroll',function(){updateSel();},true);
  window.addEventListener('resize',function(){updateSel();});
  send('veToast',{msg:'\u270f\ufe0f \u0412\u0438\u0437\u0443\u0430\u043b\u044c\u043d\u044b\u0439 \u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440 \u0433\u043e\u0442\u043e\u0432: \u043a\u043b\u0438\u043a \u2014 \u0432\u044b\u0431\u043e\u0440 \u044d\u043b\u0435\u043c\u0435\u043d\u0442\u0430'});
})();
</script>

</body></html>"""
