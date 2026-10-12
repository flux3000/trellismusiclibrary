const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const plural=(n,w)=>`${n} ${w}${n===1?'':'s'}`;
const initials=n=>String(n||'?').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase();
const years=(a,b)=>!a?'':(!b||a===b)?String(a):`${a}-${b}`;
const byName=(a,b)=>(a.sort||a.name).localeCompare(b.sort||b.name);
const inkOn=hex=>{if(!/^#[0-9a-f]{6}$/i.test(hex||''))return 'var(--t0)';
  const n=parseInt(hex.slice(1),16),f=v=>{v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4)};
  const L=.2126*f(n>>16)+.7152*f(n>>8&255)+.0722*f(n&255);return L>.4?'#141210':'#ffffff'};
document.getElementById('pal').addEventListener('change',e=>document.documentElement.setAttribute('data-palette',e.target.value));
document.getElementById('pal').value='cale';

/* photo with fallback underneath. Place files at photos/<kind>/<id>.<ext>
   (see README); a missing file just leaves the fallback showing. */
function art(d,kind,color){
  const f=`<span class="dx-ini">${esc(initials(d.name))}</span>`;
  const im=d.img?`<img src="photos/${kind}/${d.id}${esc(d.img.ext)}" alt="" loading="lazy" onerror="this.remove()">`:'';
  return `<span class="dx-art"${color?` style="--dx-fg:${esc(color)}"`:''}>${f}${im}</span>`;
}
function photo(d,kind){
  if(!d.img)return '';
  return `<span class="dx-photo"><img src="photos/${kind}/${d.id}${esc(d.img.ext)}" alt="" loading="lazy" onerror="this.parentNode.remove()"></span>`;
}

/* cfg: title, rows, row(d)->html, sorts[{id,label,cmp}], search(d)->string,
   filter{label,get(d)} optional, group{sort,key(d),label(d)} optional */
function run(cfg){
  let sortId=cfg.sorts[0].id,q='',fv='';
  const opts=cfg.filter?[...new Set(cfg.rows.map(cfg.filter.get).filter(Boolean))].sort():[];
  document.getElementById('page').innerHTML=`
   <h1 class="dx-title">${esc(cfg.title)}</h1>
   <div class="dim-bar">
     <input class="dim-search" id="q" placeholder="Filter ${esc(cfg.title.toLowerCase())}…" autocomplete="off">
     <div class="dim-sorts" id="sorts">${cfg.sorts.map(s=>`<button class="dim-sort${s.id===sortId?' active':''}" data-s="${s.id}">${esc(s.label)}</button>`).join('')}</div>
     ${cfg.filter?`<select class="dx-select" id="f"><option value="">${esc(cfg.filter.label)}</option>${opts.map(o=>`<option>${esc(o)}</option>`).join('')}</select>`:''}
     <span class="dim-count" id="n"></span>
   </div><div id="out"></div>`;
  function paint(){
    const t=q.trim().toLowerCase();
    let r=cfg.rows.filter(d=>(!t||cfg.search(d).toLowerCase().includes(t))&&(!fv||cfg.filter.get(d)===fv));
    r=r.slice().sort(cfg.sorts.find(s=>s.id===sortId).cmp);
    let h='';
    if(cfg.group&&cfg.group.sort===sortId){
      const gs=[];r.forEach(d=>{const k=cfg.group.key(d);let g=gs[gs.length-1];if(!g||g.k!==k){g={k,l:cfg.group.label(d),rows:[]};gs.push(g)}g.rows.push(d)});
      h=gs.map(g=>`<section class="dx-group"><div class="dx-gutter">${esc(g.l)}</div><div class="dx-list">${g.rows.map(cfg.row).join('')}</div></section>`).join('');
    }else h=`<div class="dx-list">${r.map(cfg.row).join('')}</div>`;
    document.getElementById('out').innerHTML=r.length?h:`<div class="dim-empty">Nothing matches.</div>`;
    document.getElementById('n').textContent=(t||fv)?`${r.length} of ${cfg.rows.length}`:'';
  }
  document.getElementById('q').addEventListener('input',e=>{q=e.target.value;paint()});
  document.getElementById('sorts').addEventListener('click',e=>{const b=e.target.closest('.dim-sort');if(!b)return;sortId=b.dataset.s;
    document.querySelectorAll('.dim-sort').forEach(x=>x.classList.toggle('active',x===b));paint()});
  if(cfg.filter)document.getElementById('f').addEventListener('change',e=>{fv=e.target.value;paint()});
  paint();
}
