/* Infinity Social — общие диалоги (заменяют prompt()/confirm())
   Использование:
     if(!await UI.confirm({title:'...', text:'...', danger:true})) return;
     const name = await UI.prompt({title:'...', label:'...', value:''});
     if(name===null) return;
*/
(function(){
  'use strict';

  const ICONS = {
    confirm: 'fa-circle-question',
    danger: 'fa-triangle-exclamation',
    prompt: 'fa-pen-to-square'
  };

  function esc(s){
    return String(s===null||s===undefined?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function ensureRoot(){
    let root=document.getElementById('uiModalRoot');
    if(!root){
      root=document.createElement('div');
      root.id='uiModalRoot';
      document.body.appendChild(root);
    }
    return root;
  }

  function closeAll(){
    const root=document.getElementById('uiModalRoot');
    if(root) root.innerHTML='';
    document.removeEventListener('keydown', onKey);
  }

  function onKey(e){
    const root=document.getElementById('uiModalRoot');
    if(!root || !root.firstElementChild) return;
    if(e.key==='Escape'){
      const btn=root.querySelector('[data-ui-cancel]');
      if(btn) btn.click(); else closeAll();
    }else if(e.key==='Enter' && root.querySelector('[data-ui-input]')){
      const btn=root.querySelector('[data-ui-ok]');
      if(btn) btn.click();
    }
  }
function render(opts){
    const root=ensureRoot();
    const isPrompt=!!opts.field;
    const danger=!!opts.danger;
    const icon=danger?ICONS.danger:(isPrompt?ICONS.prompt:ICONS.confirm);
    const okText=opts.okText||(isPrompt?'Сохранить':'ОК');
    const cancelText=opts.cancelText||'Отмена';

    const fieldHtml = isPrompt
      ? `<div class="ui-field">
           ${opts.label?`<label class="ui-label" for="uiPromptInput">${esc(opts.label)}</label>`:''}
           ${opts.multiline
             ? `<textarea id="uiPromptInput" class="ui-input" data-ui-input rows="${opts.rows||4}" maxlength="${opts.maxlength||2000}" placeholder="${esc(opts.placeholder||'')}">${esc(opts.value||'')}</textarea>`
             : `<input id="uiPromptInput" class="ui-input" data-ui-input type="text" maxlength="${opts.maxlength||200}" placeholder="${esc(opts.placeholder||'')}" value="${esc(opts.value||'')}">`}
           <div class="ui-field-foot">
             ${opts.hint?`<span class="ui-hint">${esc(opts.hint)}</span>`:'<span></span>'}
             <span class="ui-counter" data-ui-counter></span>
           </div>
         </div>`
      : `<p class="ui-text">${opts.text||''}</p>`;

    root.innerHTML = `<div class="ui-overlay" data-ui-overlay>
      <div class="ui-modal ${danger?'is-danger':''} ${opts.wide?'is-wide':''}" role="dialog" aria-modal="true">
        <div class="ui-head"><i class="fas ${icon}"></i><h3>${esc(opts.title||(isPrompt?'Введите значение':'Подтвердите действие'))}</h3></div>
        <div class="ui-body">${fieldHtml}</div>
        <div class="ui-actions">
          <button class="btn btn-secondary" data-ui-cancel>${esc(cancelText)}</button>
          <button class="btn ${danger?'btn-danger':'btn-primary'}" data-ui-ok>${esc(okText)}</button>
        </div>
      </div>
    </div>`;

    const input=root.querySelector('[data-ui-input]');
    const counter=root.querySelector('[data-ui-counter]');
    function syncCounter(){
      if(!counter) return;
      const max=parseInt(input.getAttribute('maxlength')||'0',10);
      counter.textContent = max ? (input.value.length+'/'+max) : '';
    }
    if(input){ input.addEventListener('input', syncCounter); syncCounter(); }

    return new Promise(resolve=>{
      function finish(result){ closeAll(); resolve(result); }
      root.querySelector('[data-ui-ok]').addEventListener('click', ()=>{
        if(!input) return finish(true);
        const v=input.value.trim();
        if(opts.required && !v){ input.classList.add('is-error'); input.focus(); return; }
        finish(v);
      });
      root.querySelector('[data-ui-cancel]').addEventListener('click', ()=>finish(isPrompt?null:false));
      root.querySelector('[data-ui-overlay]').addEventListener('mousedown', e=>{
        if(e.target.classList.contains('ui-overlay')) finish(isPrompt?null:false);
      });
      document.addEventListener('keydown', onKey);
      setTimeout(()=>{
        if(input){ input.focus(); if(input.select && opts.selectAll!==false) input.select(); }
        else root.querySelector('[data-ui-ok]').focus();
      },30);
    });
  }
const UI = {
    confirm(opts){
      if(typeof opts==='string') opts={title:opts};
      return render(Object.assign({danger:false}, opts||{}));
    },
    prompt(opts){
      if(typeof opts==='string') opts={title:opts, label:opts};
      return render(Object.assign({field:true, required:true}, opts||{}));
    },
    alert(opts){
      if(typeof opts==='string') opts={title:opts, text:opts};
      return render(Object.assign({okText:'Понятно'}, opts||{}));
    },
    /* Одиночный выбор из списка (заменяет prompt('Введите вариант')) */
    choose(title, options, value){
      return new Promise(resolve=>{
        closeAll();
        const root=ensureRoot();
        root.innerHTML = `<div class="ui-overlay"><div class="ui-modal" role="dialog" aria-modal="true">
          <div class="ui-head"><i class="fas fa-list-check"></i><h3>${esc(title)}</h3></div>
          <div class="ui-body"><div class="ui-choose">${options.map(o=>`
            <button class="ui-choose-item ${String(o.value)===String(value)?'is-active':''}" data-v="${esc(o.value)}">
              ${o.icon?`<i class="fas ${o.icon}"></i>`:''}<span>${esc(o.label)}</span>
            </button>`).join('')}</div></div>
          <div class="ui-actions"><button class="btn btn-secondary" data-ui-cancel>Отмена</button></div>
        </div></div>`;
        root.querySelectorAll('[data-v]').forEach(b=>b.addEventListener('click',()=>{
          const v=b.getAttribute('data-v'); closeAll(); resolve(v);
        }));
        root.querySelector('[data-ui-cancel]').addEventListener('click',()=>{closeAll();resolve(null)});
        root.querySelector('.ui-overlay').addEventListener('mousedown',e=>{
          if(e.target.classList.contains('ui-overlay')){closeAll();resolve(null)}
        });
        document.addEventListener('keydown', onKey);
      });
    }
  };

  window.UI = UI;
})();